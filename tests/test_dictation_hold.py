"""Voice dictation hold: while the voice-toggle helper records, playback waits
(nothing dropped, nothing spoken into the mic), and `interrupt` really stops the
pipeline's in-flight player."""

import asyncio
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.pipeline.generate_stage import AudioSegment  # noqa: E402
from daemon.pipeline.playback_stage import PlaybackStage, PlaybackState  # noqa: E402


def _segment(i):
    return AudioSegment(audio_path=f"/tmp/nonexistent-{i}.wav", duration_ms=10.0, text=f"line {i}",
                        session_id="S", request_id=f"r{i}", chunk_index=0, total_chunks=1,
                        generated_at=0.0)


def test_dictation_holds_then_plays_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TTS_CONTROL_DIR", str(tmp_path))
    flag = tmp_path / "dictating"
    flag.write_text("")

    async def scenario():
        stage = PlaybackStage(buffer_size=8)
        played = []

        async def fake_play(audio_path, state=None, request_id=None):
            played.append(state.current_segment.text)
            return True

        stage._play_audio = fake_play
        await stage.start_session("S")
        try:
            for i in range(2):
                await stage.enqueue_segment(_segment(i))
            await asyncio.sleep(0.6)
            held = list(played)
            flag.unlink()
            for _ in range(100):
                if len(played) == 2:
                    break
                await asyncio.sleep(0.02)
            return held, played
        finally:
            await stage.stop_session("S")

    held, played = asyncio.run(scenario())
    assert held == [], f"spoke while dictating: {held}"
    assert played == ["line 0", "line 1"], "held lines must all play, in order, once dictation ends"


def test_stop_current_terminates_only_live_players():
    stage = PlaybackStage()
    live, done = MagicMock(returncode=None), MagicMock(returncode=0)
    stage.session_states["a"] = PlaybackState()
    stage.session_states["a"].current_proc = live
    stage.session_states["b"] = PlaybackState()
    stage.session_states["b"].current_proc = done
    assert stage.stop_current() is True
    live.terminate.assert_called_once()
    done.terminate.assert_not_called()
    live.returncode = -15
    assert stage.stop_current() is False
