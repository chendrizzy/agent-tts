"""Daemon service control: find the OS service, report status, Kill / Boot / Restart.

Stdlib-only leaf; the panel reaches it through ``session_control.py daemon-*``.
The service is resolved per machine (launchd plist, systemd --user unit, else
unmanaged: pid file + socket). "Running" means the socket answers the daemon's
``health`` command, not that a file exists.

Kill sets ``control_dir()/stay_down`` and stops the service so it stays down
across login; Boot/Restart clear the flag. ``hooks/session-start.sh`` honors it.
Every service-manager call is an argv list with a timeout through ``_run`` and the
total wall time stays under the panel's 20 s limit (``BUDGET_S``).
"""
import json
import os
import platform
import plistlib
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from daemon import paths
from daemon.platforms.base import PlatformLinux

UNIT = PlatformLinux.UNIT_NAME
WAIT_S = 15      # how long a command waits for the socket to change state
BUDGET_S = 17.5  # hard ceiling for the whole command (panel timeout is 20 s)
CALL_TIMEOUT_S = 3
LAUNCHER_MAX_BYTES = 64 * 1024


class Failed(Exception):
    """The service did not reach the requested state (message is the one-line reason)."""


def _run(argv: list) -> tuple:
    """(returncode, stdout, stderr) of an argv list; never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=CALL_TIMEOUT_S)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out"
    except OSError as e:
        return 127, "", str(e)


def _spawn(argv: list, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def socket_request(payload: dict, timeout: float = 1.0) -> Optional[dict]:
    """One JSON request to the daemon socket; the parsed reply, or None if it is down/unreadable."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(paths.socket_path())
            s.sendall(json.dumps(payload).encode() + b"\n")
            buf = b""
            while len(buf) < 1 << 20:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        reply = json.loads(buf)
        return reply if isinstance(reply, dict) else None
    except (OSError, ValueError):
        return None


def _up() -> bool:
    return socket_request({"command": "health"}) is not None


# --- resolution ----------------------------------------------------------------
def _pid_file() -> Path:
    return Path.home() / ".claude" / "tts_daemon.pid"  # same path tts_daemon.PID_FILE writes


def _pid() -> Optional[int]:
    try:
        pid = int(_pid_file().read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _stay_down_path() -> Path:
    return paths.control_dir() / "stay_down"


# A command line (backslash continuations joined) that runs tts_daemon.py: an exec or a python call.
_RUNS_DAEMON = re.compile(r"^\s*(exec\s|\S*python[\d.]*\s)[^#\n]*tts_daemon\.py\b", re.M)


def _launches_daemon(args: list) -> bool:
    if any(str(a).endswith("tts_daemon.py") for a in args):
        return True
    for a in args:  # a launcher script (any argument, e.g. after /bin/sh) that runs tts_daemon.py
        try:
            f = Path(str(a))
            if f.is_file() and f.stat().st_size < LAUNCHER_MAX_BYTES \
                    and _RUNS_DAEMON.search(f.read_text(errors="replace").replace("\\\n", " ")):
                return True
        except OSError:
            continue
    return False


def _loaded(svc: dict) -> bool:
    return _run(["launchctl", "print", _gui(svc)])[0] == 0


def resolve_service() -> dict:
    """{"manager": "launchd"|"systemd"|"none", "label": str|None, "path": str|None}."""
    if platform.system() == "Darwin":
        found = []
        for plist in sorted((Path.home() / "Library" / "LaunchAgents").glob("*.plist")):
            try:
                data = plistlib.loads(plist.read_bytes())
            except Exception:
                continue
            if data.get("Label") and _launches_daemon(data.get("ProgramArguments") or []):
                found.append({"manager": "launchd", "label": data["Label"], "path": str(plist)})
        if found:  # several candidates: the one launchd has loaded wins
            return next((s for s in found if len(found) == 1 or _loaded(s)), found[0])
    elif platform.system() == "Linux":
        xdg = os.environ.get("XDG_CONFIG_HOME")
        unit = (Path(xdg) if xdg else Path.home() / ".config") / "systemd" / "user" / UNIT
        if unit.exists():
            return {"manager": "systemd", "label": UNIT, "path": str(unit)}
    return {"manager": "none", "label": None, "path": None}


def status(svc: Optional[dict] = None) -> dict:
    from daemon import session_control
    svc = svc or resolve_service()
    running = _up()
    return {"manager": svc["manager"], "label": svc["label"], "running": running,
            "pid": _pid() if running else None, "paused": session_control.is_paused(),
            "stay_down": _stay_down_path().exists(), "socket": paths.socket_path()}


# --- actions -------------------------------------------------------------------
def _wait(want_up: bool, t0: float, still: Optional[int] = None, limit: Optional[float] = None) -> bool:
    """Poll until the socket matches ``want_up`` (and the pid differs from ``still``, a pre-restart pid)."""
    deadline = min(time.monotonic() + (WAIT_S if limit is None else limit), t0 + BUDGET_S)
    while True:
        if _up() == want_up and (not want_up or still is None or _pid() != still):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def _gui(svc: dict) -> str:
    return f"gui/{os.getuid()}/{svc['label']}"


def _stop(svc: dict, t0: float, stay: bool, grace: float = 5, limit: Optional[float] = None) -> None:
    if stay:
        d = paths.control_dir()
        d.mkdir(parents=True, exist_ok=True)
        _stay_down_path().write_text(json.dumps({"since": time.time()}), encoding="utf-8")
    if svc["manager"] == "launchd":
        _run(["launchctl", "bootout", _gui(svc)])
        if stay:
            _run(["launchctl", "disable", _gui(svc)])  # RunAtLoad must not revive it at next login
    elif svc["manager"] == "systemd":
        _run(["systemctl", "--user", "disable", "--now", UNIT] if stay else ["systemctl", "--user", "stop", UNIT])
    else:
        pid = _pid()
        socket_request({"command": "shutdown"})
        if not _wait(False, t0, limit=grace) and pid:  # grace for the shutdown command, then SIGTERM
            rc, cmd, _ = _run(["ps", "-p", str(pid), "-o", "command="])
            if rc == 0 and "tts_daemon.py" in cmd:
                os.kill(pid, signal.SIGTERM)
    if not _wait(False, t0, limit=limit):
        raise Failed("daemon still answering on the socket after stop")
    if svc["manager"] == "launchd":  # bootout returns before launchd drops the job; Boot must not see it
        while _loaded(svc):
            if time.monotonic() >= t0 + BUDGET_S:
                raise Failed("launchd still has the job loaded after stop")
            time.sleep(0.25)


def _start(svc: dict, t0: float) -> None:
    _stay_down_path().unlink(missing_ok=True)
    if _up():
        return
    if svc["manager"] == "launchd":
        _run(["launchctl", "enable", _gui(svc)])
        if not _loaded(svc):  # bootstrap's exit 5 is both "already loaded" and real errors, so ask first
            rc, _, err = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", svc["path"]])
            if rc != 0:
                raise Failed(f"launchctl bootstrap failed: {err.strip().splitlines()[-1] if err.strip() else rc}")
        _run(["launchctl", "kickstart", _gui(svc)])
    elif svc["manager"] == "systemd":
        _run(["systemctl", "--user", "enable", "--now", UNIT])
    else:
        root = Path(__file__).resolve().parent.parent
        python = os.environ.get("CLAUDE_TTS_PYTHON") or sys.executable  # same first choice as session-start.sh
        _spawn([python, str(root / "daemon" / "tts_daemon.py")], root / "logs" / "tts_daemon.log")
    if not _wait(True, t0):
        raise Failed("daemon did not answer on the socket within 15 s")


def _restart(svc: dict, t0: float) -> None:
    _stay_down_path().unlink(missing_ok=True)
    old = _pid()
    if svc["manager"] == "launchd" and _loaded(svc):
        _run(["launchctl", "kickstart", "-k", _gui(svc)])
    elif svc["manager"] == "systemd":
        _run(["systemctl", "--user", "enable", UNIT])
        _run(["systemctl", "--user", "restart", UNIT])
    else:
        if _up():  # keep stop short (<= 5 s) so start still gets >= 12 s of the budget
            _stop(svc, t0, stay=False, grace=3, limit=2)
        return _start(svc, t0)
    if not _wait(True, t0, still=old):
        raise Failed("daemon did not come back within 15 s")


def run(action: str) -> dict:
    """status | start | stop | restart -> the status dict; raises Failed if the state was not reached."""
    t0 = time.monotonic()
    svc = resolve_service()
    if action == "start":
        _start(svc, t0)
    elif action == "stop":
        try:
            _stop(svc, t0, stay=True)
        except Failed:  # still running: undo stay-down, or its next crash would look like a Kill
            _stay_down_path().unlink(missing_ok=True)
            if svc["manager"] == "launchd":
                _run(["launchctl", "enable", _gui(svc)])
            elif svc["manager"] == "systemd":
                _run(["systemctl", "--user", "enable", UNIT])
            raise
    elif action == "restart":
        _restart(svc, t0)
    return status(svc)
