"""The Stop hook forwards the session's folder, so the daemon can file the spoken
line under its project. Without it, a session that only speaks at Stop (every
headless `claude -p` run) showed up as an unplaced `session <id>`."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "_stop_event.py"


def _run_hook(payload: dict, cwd: str) -> dict:
    # AF_UNIX paths are short on macOS, so keep the socket out of pytest's long tmp dirs.
    sock_dir = tempfile.mkdtemp(prefix="se")
    path = os.path.join(sock_dir, "s")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    got = {}

    def serve():
        conn, _ = server.accept()
        got["line"] = conn.makefile().readline()
        conn.sendall(b'{"status":"ok"}\n')
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    subprocess.run([sys.executable, str(HOOK), path], input=json.dumps(payload), text=True, cwd=cwd, timeout=10)
    t.join(5)
    server.close()
    return json.loads(got["line"])


def _transcript(tmp_path: Path) -> str:
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}}) + "\n")
    return str(t)


def test_stop_event_carries_the_hook_inputs_cwd(tmp_path):
    sent = _run_hook({"session_id": "s1", "cwd": "/work/proj", "transcript_path": _transcript(tmp_path)}, str(tmp_path))
    assert sent["command"] == "stop_event"
    assert sent["cwd"] == "/work/proj"


def test_stop_event_falls_back_to_the_hooks_working_dir(tmp_path):
    sent = _run_hook({"session_id": "s1", "transcript_path": _transcript(tmp_path)}, str(tmp_path))
    assert os.path.realpath(sent["cwd"]) == os.path.realpath(str(tmp_path))
