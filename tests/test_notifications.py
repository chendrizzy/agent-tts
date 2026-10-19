"""Notification prefs: file/CLI, disk-alert modes, hook skip + kind parity, daemon drop.

Hermetic: AGENT_TTS_CONTROL_DIR -> tmp dir; hooks run as bash with a tmp HOME/socket.
"""
import json
import os
import socket as socketlib
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daemon import session_control as sc  # noqa: E402
from daemon.pipeline import generate_stage  # noqa: E402
from daemon.schema_validator import validate_event  # noqa: E402

CLI = ROOT / "daemon" / "session_control.py"
HOOK = ROOT / "hooks" / "speech_output_hook.sh"
DEFAULTS = {"disk_alert": "popup", "disk_alert_every_s": 60,
            "speak": {"permission_prompt": True, "idle_prompt": True, "other": True}}
CASES = [  # (payload fields, expected kind)
    ({"notification_type": "idle_prompt", "message": "hello"}, "idle_prompt"),
    ({"message": "Claude needs your permission to use Bash"}, "permission_prompt"),
    ({"message": "Claude is waiting for your input"}, "idle_prompt"),
    ({"message": "Task finished"}, "other"),
]


@pytest.fixture(autouse=True)
def ctl(tmp_path, monkeypatch):
    d = tmp_path / "ctl"
    monkeypatch.setenv("AGENT_TTS_CONTROL_DIR", str(d))
    sc._PREFS_CACHE.update(sig=None, prefs=None)
    return d


def _cli(ctl, *args):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True,
                          env={**os.environ, "AGENT_TTS_CONTROL_DIR": str(ctl)})


# --- prefs -------------------------------------------------------------------
def test_defaults_when_missing_or_corrupt(ctl):
    assert sc.notification_prefs() == DEFAULTS
    ctl.mkdir()
    (ctl / "notifications.json").write_text("{not json")
    assert sc.notification_prefs() == DEFAULTS
    (ctl / "notifications.json").write_text(json.dumps(
        {"disk_alert": "bogus", "disk_alert_every_s": 5, "speak": {"other": False, "idle_prompt": "no"}}))
    assert sc.notification_prefs() == {**DEFAULTS, "speak": {**DEFAULTS["speak"], "other": False}}


def test_set_validates_and_persists_and_is_read_live(ctl):
    assert sc.notification_prefs() == DEFAULTS
    out = sc.set_notification_prefs({"disk_alert": "log", "disk_alert_every_s": "900", "speak.idle_prompt": "false"})
    assert out == {"disk_alert": "log", "disk_alert_every_s": 900,
                   "speak": {"permission_prompt": True, "idle_prompt": False, "other": True}}
    assert sc.notification_prefs() == out  # re-read after write (cache sig changed)
    assert not sc.allow_notification("idle_prompt") and sc.allow_notification("other")
    assert not list(ctl.glob("*.tmp*"))


@pytest.mark.parametrize("key,value", [
    ("disk_alert", "loud"), ("disk_alert_every_s", "30"), ("disk_alert_every_s", "x"),
    ("speak.idle_prompt", "maybe"), ("speak.nope", "true"), ("nope", "1"),
])
def test_set_rejects_invalid_without_writing(ctl, key, value):
    with pytest.raises(ValueError, match=key.split(".")[0]):
        sc.set_notification_prefs({"disk_alert": "off", key: value})
    assert not (ctl / "notifications.json").exists()


def test_cli_get_set_and_errors(ctl):
    r = _cli(ctl, "notifications")
    assert r.returncode == 0 and json.loads(r.stdout) == {"prefs": DEFAULTS}
    r = _cli(ctl, "notifications", "--set", "disk_alert=off", "--set", "speak.other=false")
    assert r.returncode == 0
    prefs = json.loads(r.stdout)["prefs"]
    assert prefs["disk_alert"] == "off" and prefs["speak"]["other"] is False
    r = _cli(ctl, "notifications", "--set", "speak.idle_prompt=maybe")
    assert r.returncode == 2 and "speak.idle_prompt" in json.loads(r.stdout)["error"]
    r = _cli(ctl, "notifications", "--set", "novalue")
    assert r.returncode == 2 and "error" in json.loads(r.stdout)
    assert json.loads(_cli(ctl, "notifications").stdout)["prefs"] == prefs  # failures changed nothing


@pytest.mark.parametrize("fields,kind", CASES)
def test_notification_kind(fields, kind):
    assert sc.notification_kind(fields) == kind
    assert sc.notification_kind(fields.get("message")) == (kind if "notification_type" not in fields else "other")


# --- disk alert --------------------------------------------------------------
@pytest.fixture
def stage():
    s = generate_stage.GenerateStage.__new__(generate_stage.GenerateStage)
    s._last_disk_warn = 0.0
    return s


@pytest.fixture
def sinks(monkeypatch):
    popen, append = MagicMock(), MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr("daemon.spoken_log.append", append)
    monkeypatch.setattr(sys, "platform", "darwin")
    return popen, append


@pytest.mark.parametrize("mode,popups,logs", [("popup", 1, 1), ("log", 0, 1), ("off", 0, 0)])
def test_disk_alert_modes(stage, sinks, caplog, mode, popups, logs):
    sc.set_notification_prefs({"disk_alert": mode})
    stage._signal_disk_full("s1", 5 * 1024 * 1024)
    popen, append = sinks
    assert (popen.call_count, append.call_count) == (popups, logs)
    assert "DISK GUARD" in caplog.text  # logger.error is kept in every mode


def test_disk_alert_interval(stage, sinks, monkeypatch):
    popen, _ = sinks
    now = [1000.0]
    monkeypatch.setattr(generate_stage.time, "time", lambda: now[0])
    stage._signal_disk_full("s", 0)
    now[0] += 61
    stage._signal_disk_full("s", 0)            # default 60 s -> fires again
    assert popen.call_count == 2
    sc.set_notification_prefs({"disk_alert_every_s": "900"})
    now[0] += 61
    stage._signal_disk_full("s", 0)            # 61 s < 900 s -> throttled
    assert popen.call_count == 2
    now[0] += 900
    stage._signal_disk_full("s", 0)
    assert popen.call_count == 3


# --- hook --------------------------------------------------------------------
def _run_hook(tmp_path, ctl, payload):
    sock_dir = tempfile.mkdtemp(dir="/tmp")
    srv = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
    srv.bind(os.path.join(sock_dir, "s.sock"))
    srv.listen(4)
    srv.settimeout(1.5)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    p = subprocess.run(
        ["bash", str(HOOK)], input=json.dumps({"hook_event_name": "Notification", "session_id": "s1", **payload}).encode(),
        capture_output=True, timeout=30,
        env={**os.environ, "HOME": str(home), "CLAUDE_TTS_SOCKET": os.path.join(sock_dir, "s.sock"),
             "CLAUDE_TTS_ENABLED": "true", "AGENT_TTS_CONTROL_DIR": str(ctl)})
    return p, srv, "".join(f.read_text() for f in (home / ".claude/logs/hooks").glob("*.log"))


def test_hook_skips_disabled_idle_prompt_without_contacting_socket(tmp_path, ctl):
    sc.set_notification_prefs({"speak.idle_prompt": "false"})
    p, srv, log = _run_hook(tmp_path, ctl, {"notification_type": "idle_prompt", "message": "x"})
    assert p.returncode == 0
    with pytest.raises(socketlib.timeout):
        srv.accept()
    srv.close()
    assert "notification skipped: idle_prompt off" in log


def test_hook_passes_enabled_kind_and_forwards_kind(tmp_path, ctl):
    sc.set_notification_prefs({"speak.permission_prompt": "false"})  # other kinds stay on
    tr = tmp_path / "t.jsonl"
    tr.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Latest reply."}]}}) + "\n")
    p, srv, _ = _run_hook(tmp_path, ctl, {"notification_type": "idle_prompt", "message": "Claude is waiting for your input", "transcript_path": str(tr)})
    conn, _ = srv.accept()
    conn.settimeout(3)
    got = b""
    while not got.endswith(b"\n"):
        chunk = conn.recv(4096)
        if not chunk:
            break
        got += chunk
    conn.close()
    srv.close()
    assert p.returncode == 0
    sent = json.loads(got)
    assert sent["notification_kind"] == "idle_prompt"
    assert sent["content"] == "Claude is waiting for your input"  # not the transcript's last reply


@pytest.mark.parametrize("fields,kind", CASES)
def test_hook_kind_agrees_with_python(tmp_path, ctl, fields, kind):
    """All kinds off: the hook's logged kind must equal notification_kind()."""
    sc.set_notification_prefs({f"speak.{k}": "false" for k in sc.NOTIFICATION_KINDS})
    p, srv, log = _run_hook(tmp_path, ctl, fields)
    srv.close()
    assert sc.notification_kind(fields) == kind
    assert f"notification skipped: {kind} off" in log


# --- daemon ------------------------------------------------------------------
def test_stop_event_schema_accepts_notification_kind():
    ok, why = validate_event({"command": "stop_event", "session_id": "s", "content": "c",
                              "event_id": "e", "ts": 1.0, "notification_kind": "idle_prompt"}, "stop_event")
    assert ok, why


def _daemon_reply(payload, monkeypatch):
    from daemon import tts_daemon
    d = tts_daemon.TTSDaemon.__new__(tts_daemon.TTSDaemon)
    d.log = lambda *_a, **_k: None
    sent = []
    cs = MagicMock()
    cs.send.side_effect = sent.append
    monkeypatch.setattr(tts_daemon, "_recv_line", lambda _s: json.dumps(payload).encode() + b"\n")
    d.handle_client(cs)
    return json.loads(b"".join(sent))


def test_daemon_drops_disallowed_notification_kind(monkeypatch):
    from daemon import tts_daemon
    sc.set_notification_prefs({"speak.idle_prompt": "false"})
    ev = {"command": "stop_event", "session_id": "s", "content": "hi", "event_id": "e1", "ts": 1.0,
          "notification_kind": "idle_prompt"}
    assert _daemon_reply(ev, monkeypatch) == {"status": "ok", "event_id": "e1", "queued": False, "reason": "notification off"}
