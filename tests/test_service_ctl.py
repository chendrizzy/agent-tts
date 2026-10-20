"""Service resolution and Kill/Boot/Restart sequences -- every service call is mocked."""

import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon import service_ctl as sv  # noqa: E402
from daemon import session_control as sc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UID = os.getuid()


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_TTS_CONTROL_DIR", str(tmp_path / "ctl"))
    monkeypatch.setenv("CLAUDE_TTS_SOCKET", str(tmp_path / "s.sock"))
    monkeypatch.setattr(sv.time, "sleep", lambda s: None)
    monkeypatch.setattr(sv, "WAIT_S", 0.05)  # sleep is a no-op, so waits would otherwise spin for real seconds
    return home


def _plist(home, name, label, args):
    p = home / "Library" / "LaunchAgents" / name
    p.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": args}))
    return p


class Fake:
    """Mocked runner + socket: ``up`` flips as service commands run."""

    def __init__(self, monkeypatch, up=True, on=None):
        self.calls, self.up, self.on, self.spawned, self.requests, self.pid = [], up, on or {}, [], [], 4242
        self.loaded = up  # what `launchctl print` reports
        monkeypatch.setattr(sv, "_run", self.run)
        monkeypatch.setattr(sv, "_spawn", lambda argv, log: (self.spawned.append(argv), setattr(self, "up", True)))
        monkeypatch.setattr(sv, "socket_request", self.request)
        monkeypatch.setattr(sv, "_pid", lambda: self.pid if self.up else None)

    def run(self, argv):
        self.calls.append(argv)
        if "-k" in argv or "restart" in argv:
            self.pid += 1  # a restarted daemon is a new process
        key = next((k for k in self.on if k in " ".join(argv)), None)
        if key:
            self.up = self.on[key]
        if "bootout" in argv:
            self.loaded = False
        if argv[:2] == ["launchctl", "print"]:
            return (0 if self.loaded else 113), "", ""
        return 0, "", ""

    def request(self, payload, timeout=1.0):
        self.requests.append(payload["command"])
        if payload["command"] == "shutdown":
            self.up = False
            return {"status": "success"}
        return {"status": "ok"} if self.up else None


def test_resolve_direct_arg(env, monkeypatch):
    monkeypatch.setattr(sv.platform, "system", lambda: "Darwin")
    _plist(env, "a.plist", "com.other.thing", ["/bin/echo"])
    p = _plist(env, "b.plist", "com.x.tts", ["/usr/bin/python3", "/opt/claude-tts/daemon/tts_daemon.py"])
    assert sv.resolve_service() == {"manager": "launchd", "label": "com.x.tts", "path": str(p)}


def test_resolve_launcher_script_arg(env, monkeypatch):
    monkeypatch.setattr(sv.platform, "system", lambda: "Darwin")
    launcher = env / "launch"
    launcher.write_text("#!/bin/sh\nexec python3 /x/daemon/tts_daemon.py\n")
    p = _plist(env, "c.plist", "com.x.launch", [str(launcher)])
    assert sv.resolve_service()["path"] == str(p)
    launcher.write_text("#!/bin/sh\n" + "# pad\n" * 20000 + "tts_daemon.py\n")  # >= 64 KB is not a launcher
    assert sv.resolve_service()["manager"] == "none"


def test_resolve_no_match_and_linux(env, monkeypatch):
    monkeypatch.setattr(sv.platform, "system", lambda: "Darwin")
    _plist(env, "a.plist", "com.other", ["/bin/echo"])
    assert sv.resolve_service() == {"manager": "none", "label": None, "path": None}
    monkeypatch.setattr(sv.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(env / "cfg"))
    unit = env / "cfg" / "systemd" / "user" / "claude-tts.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    assert sv.resolve_service() == {"manager": "systemd", "label": "claude-tts.service", "path": str(unit)}


def _launchd(env, monkeypatch):
    monkeypatch.setattr(sv.platform, "system", lambda: "Darwin")
    return _plist(env, "b.plist", "com.x.tts", ["/a/daemon/tts_daemon.py"])


def test_launchd_kill_boot_restart(env, monkeypatch):
    plist = _launchd(env, monkeypatch)
    gui = f"gui/{UID}/com.x.tts"
    f = Fake(monkeypatch, up=True, on={"bootout": False})
    out = sv.run("stop")
    assert f.calls == [["launchctl", "bootout", gui], ["launchctl", "disable", gui], ["launchctl", "print", gui]]
    assert out["running"] is False and out["stay_down"] is True and out["manager"] == "launchd"

    f = Fake(monkeypatch, up=False, on={"kickstart": True})
    out = sv.run("start")
    assert f.calls == [["launchctl", "enable", gui], ["launchctl", "print", gui],
                       ["launchctl", "bootstrap", f"gui/{UID}", str(plist)], ["launchctl", "kickstart", gui]]
    assert out["running"] is True and out["stay_down"] is False

    f = Fake(monkeypatch, up=True)
    sv.run("restart")
    assert f.calls == [["launchctl", "print", gui], ["launchctl", "kickstart", "-k", gui]]


def test_launchd_restart_when_not_loaded_boots(env, monkeypatch):
    _launchd(env, monkeypatch)
    f = Fake(monkeypatch, up=False, on={"kickstart": True})
    sv.run("restart")
    assert [c[1] for c in f.calls] == ["print", "enable", "print", "bootstrap", "kickstart"]


def test_launchd_bootstrap_error_is_reported(env, monkeypatch):
    _launchd(env, monkeypatch)
    f = Fake(monkeypatch, up=False)
    run = f.run
    monkeypatch.setattr(sv, "_run", lambda argv: (5, "", "Bootstrap failed: 5: Input/output error\n")
                        if "bootstrap" in argv else run(argv))
    with pytest.raises(sv.Failed, match="Input/output error"):
        sv.run("start")
    assert not any("kickstart" in c for c in f.calls)


def test_resolve_live_launcher_layouts(env, monkeypatch):
    """The live layout: a plist naming a launcher (possibly behind /bin/sh) that execs python on a continued line."""
    monkeypatch.setattr(sv.platform, "system", lambda: "Darwin")
    launcher = env / "claude-tts-daemon-launch"
    launcher.write_text('#!/bin/sh\nexport X=1\nexec /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \\\n'
                        '  /Users/x/.claude/claude-tts/daemon/tts_daemon.py\n')
    toggle = env / "toggle.sh"  # only names the daemon in a comment / a health check: not a launcher
    toggle.write_text("#!/bin/sh\n# pauses tts_daemon.py while dictating\npgrep -f tts_daemon.py\nexec /x/voice-toggle\n")
    _plist(env, "a.plist", "com.x.toggle", ["/bin/sh", str(toggle)])
    p = _plist(env, "b.plist", "com.x.tts", ["/bin/sh", str(launcher)])
    assert sv.resolve_service()["path"] == str(p)


def test_resolve_prefers_the_loaded_candidate(env, monkeypatch):
    monkeypatch.setattr(sv.platform, "system", lambda: "Darwin")
    _plist(env, "a.plist", "com.old.tts", ["/old/daemon/tts_daemon.py"])
    p = _plist(env, "b.plist", "com.new.tts", ["/new/daemon/tts_daemon.py"])
    monkeypatch.setattr(sv, "_run", lambda argv: (0 if argv[-1].endswith("com.new.tts") else 113, "", ""))
    assert sv.resolve_service()["path"] == str(p)


def test_systemd_sequences(env, monkeypatch):
    monkeypatch.setattr(sv.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(env / "cfg"))
    unit = env / "cfg" / "systemd" / "user" / "claude-tts.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("")
    u = "claude-tts.service"
    f = Fake(monkeypatch, up=True, on={"disable": False})
    sv.run("stop")
    assert f.calls == [["systemctl", "--user", "disable", "--now", u]]
    f = Fake(monkeypatch, up=False, on={"enable --now": True})
    sv.run("start")
    assert f.calls == [["systemctl", "--user", "enable", "--now", u]]
    f = Fake(monkeypatch, up=True)
    sv.run("restart")
    assert f.calls == [["systemctl", "--user", "enable", u], ["systemctl", "--user", "restart", u]]


def test_unmanaged_sequences(env, monkeypatch):
    monkeypatch.setattr(sv.platform, "system", lambda: "Other")
    f = Fake(monkeypatch, up=True)
    out = sv.run("stop")
    assert f.requests[0] == "shutdown" and f.calls == [] and out["running"] is False and out["stay_down"]
    f = Fake(monkeypatch, up=False)
    out = sv.run("start")
    assert len(f.spawned) == 1 and f.spawned[0][-1].endswith("tts_daemon.py") and out["running"]
    monkeypatch.setenv("CLAUDE_TTS_PYTHON", "/opt/py/bin/python3")  # the hook's interpreter wins over ours
    f = Fake(monkeypatch, up=False)
    sv.run("start")
    assert f.spawned[0][0] == "/opt/py/bin/python3"
    f = Fake(monkeypatch, up=True)
    sv.run("restart")
    assert "shutdown" in f.requests and len(f.spawned) == 1


def test_stop_failure_exits_1(env, monkeypatch, capsys):
    _launchd(env, monkeypatch)
    f = Fake(monkeypatch, up=True)  # bootout does not take it down
    assert sc.main(["daemon-stop"]) == 1
    assert capsys.readouterr().err.strip() == "daemon still answering on the socket after stop"
    gui = f"gui/{UID}/com.x.tts"
    assert not (sv.paths.control_dir() / "stay_down").exists()  # a failed Kill does not leave it flagged down
    assert ["launchctl", "enable", gui] == f.calls[-1]  # ...nor disabled at the next login


def test_status_shape(env, monkeypatch, capsys):
    _launchd(env, monkeypatch)
    Fake(monkeypatch, up=True)
    assert sc.main(["daemon-status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"manager": "launchd", "label": "com.x.tts", "running": True, "pid": 4242,
                   "paused": False, "stay_down": False, "socket": sv.paths.socket_path()}


def test_session_start_honors_stay_down(tmp_path):
    """stay_down: the hook exits 0 and starts nothing (stub python3/launchctl/nohup record calls)."""
    stubs, log, ctl = tmp_path / "bin", tmp_path / "calls.log", tmp_path / "ctl"
    stubs.mkdir()
    ctl.mkdir()
    (ctl / "stay_down").write_text("{}")
    for name in ("python3", "launchctl", "nohup"):
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 1\n')
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(tmp_path), "AGENT_TTS_CONTROL_DIR": str(ctl),
           "CLAUDE_TTS_SOCKET": str(tmp_path / "s.sock"), "CLAUDE_TTS_PYTHON": str(stubs / "python3")}
    r = subprocess.run(["bash", str(ROOT / "hooks" / "session-start.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "stay_down" in r.stderr
    assert not log.exists(), log.read_text()


def test_launchd_kill_waits_until_the_job_is_unloaded(env, monkeypatch):
    # Live: bootout returned while launchd still listed the job, so an immediate Boot skipped
    # bootstrap, kickstarted a dying job, and the daemon never came back.
    _launchd(env, monkeypatch)
    f = Fake(monkeypatch, up=True, on={"bootout": False})
    prints = {"n": 0}
    run = f.run

    def lagging(argv):
        if argv[:2] == ["launchctl", "print"]:
            prints["n"] += 1
            return (0 if prints["n"] < 3 else 113), "", ""  # still listed for two polls
        return run(argv)

    monkeypatch.setattr(sv, "_run", lagging)
    sv.run("stop")
    assert prints["n"] == 3
