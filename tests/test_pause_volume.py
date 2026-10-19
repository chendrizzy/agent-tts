"""Global pause (drops lines) and per-segment scoped volume in the playback loop."""

import asyncio
import os
from dataclasses import replace
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon import session_control as sc  # noqa: E402
from daemon.pipeline.generate_stage import AudioSegment  # noqa: E402
from daemon.pipeline.playback_stage import PlaybackStage  # noqa: E402


@pytest.fixture(autouse=True)
def control(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TTS_CONTROL_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_TTS_SOCKET", str(tmp_path / "none.sock"))  # pause() must not touch a live daemon
    return tmp_path


def _segment(i):
    return AudioSegment(audio_path=f"/tmp/nonexistent-{i}.wav", duration_ms=10.0, text=f"line {i}",
                        session_id="S", request_id=f"r{i}", chunk_index=0, total_chunks=1,
                        generated_at=0.0)


def _play(master, texts=(0,), setup=lambda: None):
    """Run the loop over segments; return the (text, volume) pairs that reached the player."""
    async def scenario():
        stage = PlaybackStage(buffer_size=8, volume=master)
        played = []

        async def fake_play(audio_path, state=None, request_id=None, volume=None):
            played.append((state.current_segment.text, volume))
            return True

        stage._play_audio = fake_play
        setup()
        await stage.start_session("S")
        try:
            for i in texts:
                await stage.enqueue_segment(_segment(i))
            await asyncio.sleep(0.8)
        finally:
            await stage.stop_session("S")
        return played

    return asyncio.run(scenario())


def test_pause_resume_flag_and_snapshot():
    assert sc.is_paused() is False
    sc.pause()  # daemon is down: must not raise
    assert sc.is_paused() is True
    assert sc._snapshot("{}")["paused"] is True
    sc.resume()
    sc.resume()  # idempotent
    assert sc.is_paused() is False


def test_playback_drops_while_paused():
    assert _play(1.0, setup=sc.pause) == []
    sc.resume()
    assert [t for t, _ in _play(1.0)] == ["line 0"]


def test_pause_cli(capsys):
    assert sc.main(["pause"]) == 0
    assert sc.is_paused()
    assert sc.main(["resume"]) == 0
    assert not sc.is_paused()


def _handle(payload):
    """Drive the daemon's real handle_client with one request line; return the decoded reply."""
    import json
    import logging
    from unittest.mock import MagicMock, patch
    from daemon.tts_daemon import TTSDaemon
    d = TTSDaemon.__new__(TTSDaemon)
    d.logger = logging.getLogger("tts_daemon_test_pause_volume")
    d.log = lambda msg, level="INFO": None
    d.stats = {"requests_received": 0}
    d._handle_tool_event = d._handle_stop_event = MagicMock(side_effect=AssertionError("routed"))
    sent = []
    sock = MagicMock()
    sock.send.side_effect = sent.append
    with patch("daemon.tts_daemon._recv_line", return_value=(json.dumps(payload) + "\n").encode()):
        d.handle_client(sock)
    return json.loads(sent[0])


@pytest.mark.parametrize("extra", [{"command": "tool_event"}, {"command": "stop_event"},
                                   {"command": "stop_event", "notification_kind": "permission"}])
def test_daemon_drops_events_while_paused(extra):
    sc.mute(["S"])  # paused wins over muted
    sc.pause()
    reply = _handle({"session_id": "S", "event_id": "e1", **extra})
    assert reply == {"status": "ok", "event_id": "e1", "queued": False, "reason": "paused"}
    sc.resume()
    reply = _handle({"session_id": "S", "event_id": "e2", **extra})
    assert reply["queued"] is False and reply["reason"] == "muted"


def test_pause_sends_interrupt(monkeypatch):
    from unittest.mock import MagicMock
    from daemon import service_ctl
    req = MagicMock()
    monkeypatch.setattr(service_ctl, "socket_request", req)
    sc.pause()
    req.assert_called_once_with({"command": "interrupt"})


def test_segment_waiting_on_audio_lock_is_dropped_by_pause(monkeypatch, tmp_path):
    """Pause set while session B waits on the cross-session lock: B's player is never spawned."""
    spawned = []
    release = asyncio.Event

    class Proc:
        returncode = None
        pid = 1

        def __init__(self, gate):
            self.gate = gate

        async def wait(self):
            await self.gate.wait()
            self.returncode = 0
            return 0

        def terminate(self):
            pass

    async def scenario():
        gate = release()

        async def fake_exec(*args, **kw):
            spawned.append(args)
            return Proc(gate)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        stage = PlaybackStage(buffer_size=8, volume=1.0)
        await stage.start_session("A")
        await stage.start_session("B")
        try:
            wav = tmp_path / "a.wav"
            wav.write_bytes(b"x")
            seg = replace(_segment(0), audio_path=str(wav))
            await stage.enqueue_segment(replace(seg, session_id="A", request_id="ra"))
            await asyncio.sleep(0.3)  # A holds the lock inside its player
            assert len(spawned) == 1
            await stage.enqueue_segment(replace(seg, session_id="B", request_id="rb"))
            await asyncio.sleep(0.3)  # B staged, waiting on the lock
            sc.pause()
            gate.set()
            await asyncio.sleep(0.8)
        finally:
            await stage.stop_session("A")
            await stage.stop_session("B")
        return stage

    stage = asyncio.run(scenario())
    assert len(spawned) == 1
    assert stage._stats["playback_errors"] == 0


@pytest.mark.parametrize("master,rule,expect", [
    (1.0, None, 1.0),    # no rule: master only
    (0.5, 1.5, 0.75),    # master x scoped
    (2.0, 2.0, 2.0),     # clamped into 0..2
    (1.0, 0, None),      # zero drops the segment like mute
])
def test_scoped_volume_resolution(master, rule, expect):
    def setup():
        if rule is not None:
            sc.set_rule("session", "S", rule, "volume")

    played = _play(master, setup=setup)
    assert played == ([] if expect is None else [("line 0", pytest.approx(expect))])
