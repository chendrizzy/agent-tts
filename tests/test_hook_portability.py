"""Hooks must deliver on a STOCK machine, and must not lie when they don't.

Why this file exists (2026-10-11): every shipped release wrapped each hook's
inline Python in GNU coreutils `timeout`. macOS does not ship `timeout`. On a
stock Mac — the primary platform, since the Kokoro engine is Apple-Silicon —
the shell printed "timeout: command not found", the Python never ran, nothing
reached the daemon, and the hook logged "stop_event dispatched" and exited 0.
It worked only on machines with Homebrew coreutils, which the author's has.

Three releases shipped it because no test ever ran a hook end to end. The
tests below are written to fail on that bug specifically:

  * every assertion is about what the DAEMON RECEIVED, never the hook's exit
    code — the hook exited 0 the whole time it was delivering nothing;
  * the child environment is an explicit minimal PATH (/usr/bin:/bin), which
    is what a stock macOS user has, not the developer's PATH;
  * a source-level guard fails on any external command in a hook outside a
    short allowlist of POSIX-guaranteed tools, so the next non-portable
    dependency is caught before it ships rather than after.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

HOOKS = PROJECT_ROOT / "hooks"

# A stock macOS / minimal Linux PATH. Deliberately excludes /opt/homebrew/bin
# and /usr/local/bin: the bug this file guards was invisible with those on PATH.
STOCK_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


# The listener runs from a file with this exact name because
# ensure-daemon-ready.sh gates on `pgrep -f tts_daemon.py` and treats a socket
# with no matching process as orphaned -- it unlinks it and the hook skips
# delivery. A thread cannot satisfy that; a real process can.
_FAKE_DAEMON_SRC = r"""
import json, os, socket, sys

sock_path, out_path, expect = sys.argv[1], sys.argv[2], int(sys.argv[3])
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
srv.listen(8)
srv.settimeout(25)
received = []
try:
    while len(received) < expect:
        conn, _addr = srv.accept()
        with conn:
            data = conn.recv(65536)
            if data:
                received.append(data.decode("utf-8", "replace"))
            try:
                conn.sendall(b'{"status":"ok"}\n')
            except OSError:
                pass
except Exception:
    pass
finally:
    srv.close()
    with open(out_path, "w") as fh:
        json.dump(received, fh)
"""


class _FakeDaemon:
    """A real `tts_daemon.py` process holding a real AF_UNIX socket.

    Faithful to the hooks' precondition rather than a shortcut around it: the
    hooks require a live process of that name, and will delete an unattended
    socket.
    """

    def __init__(self, expect: int = 1):
        # AF_UNIX paths cap near 104 bytes; pytest's tmp_path is deeper.
        self.dir = tempfile.mkdtemp(prefix="ttsport-", dir="/tmp")
        self.path = os.path.join(self.dir, "d.sock")
        self._out = os.path.join(self.dir, "received.json")
        self._received: list[str] | None = None
        script = os.path.join(self.dir, "tts_daemon.py")
        with open(script, "w") as fh:
            fh.write(_FAKE_DAEMON_SRC)
        self._proc = subprocess.Popen(
            [sys.executable, script, self.path, self._out, str(expect)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        # Wait for the socket to appear; a hook that arrives first sees no
        # socket and correctly skips, which would be a false failure.
        for _ in range(200):
            if os.path.exists(self.path):
                return
            time.sleep(0.05)
        raise AssertionError("fake daemon never bound its socket")

    @property
    def received(self) -> list[str]:
        # Snapshotted by close(), because close() removes the directory the
        # results file lives in -- reading it afterwards always returned [].
        if self._received is not None:
            return self._received
        if os.path.exists(self._out):
            with open(self._out) as fh:
                return json.load(fh)
        return []

    def close(self, wait: float = 25.0) -> None:
        try:
            self._proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        self._received = self.received
        shutil.rmtree(self.dir, ignore_errors=True)


def _have(cmd: str) -> bool:
    return shutil.which(cmd, path=STOCK_PATH) is not None


def _run_hook(script: Path, payload: dict, home: Path, sock: str):
    # `bash`, not `sh`: every hook carries #!/bin/bash and hooks.json invokes
    # `bash ${CLAUDE_PLUGIN_ROOT}/hooks/...`. Running them with `sh` tests a
    # contract the product does not make -- on Ubuntu, where /bin/sh is dash,
    # it reports "[[: not found" for legitimate bash. See
    # test_hooks_are_invoked_as_bash_everywhere below, which pins this.
    return subprocess.run(
        ["bash", str(script)],
        input=json.dumps(payload).encode(),
        env={
            "PATH": STOCK_PATH,
            "HOME": str(home),
            "CLAUDE_TTS_SOCKET": sock,
            "CLAUDE_TTS_ENABLED": "true",
        },
        capture_output=True,
        timeout=60,
    )


def _hook_log(home: Path) -> str:
    logs = sorted((home / ".claude" / "logs" / "hooks").glob("*.log"))
    return "\n".join(p.read_text(errors="replace") for p in logs)


def test_stop_hook_delivers_on_a_stock_path(tmp_path):
    """The turn-summary path must reach the daemon with no Homebrew on PATH.

    This is the regression test for the `timeout`-not-found bug. It asserts the
    daemon's received count, because the hook's own exit code was 0 throughout.
    """
    tr = tmp_path / "t.jsonl"
    tr.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "the stop path is alive"}]},
    }) + "\n")
    daemon = _FakeDaemon(expect=1)
    proc = _run_hook(
        HOOKS / "speech_output_hook.sh",
        {"session_id": "s1", "transcript_path": str(tr), "stop_hook_active": False},
        tmp_path, daemon.path,
    )
    daemon.close()
    assert len(daemon.received) == 1, (
        "stop hook delivered nothing on a stock PATH — the daemon received "
        f"{len(daemon.received)} messages.\n"
        f"rc={proc.returncode} stderr={proc.stderr[:600]!r}\n"
        f"hook log: {_hook_log(tmp_path)[:1200]!r}"
    )
    msg = json.loads(daemon.received[0])
    assert msg["command"] == "stop_event"
    assert msg["content"] == "the stop path is alive"


def test_post_tool_hook_delivers_on_a_stock_path(tmp_path):
    """The tool-event path must reach the daemon with no Homebrew on PATH.

    This hook needs `jq`. Stock macOS ships it (/usr/bin/jq) and macOS is the
    primary platform, but minimal Linux images do not. Rather than skip there,
    assert the honest-degradation contract: say why in the log, and do not
    claim a delivery that did not happen. That second half is the behaviour
    whose absence hid the `timeout` outage for three releases.
    """
    daemon = _FakeDaemon(expect=1)
    proc = _run_hook(
        HOOKS / "post-tool-use.sh",
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
            "tool_response": {"stdout": "3 passed in 0.1s", "stderr": "",
                              "interrupted": False},
            "tool_use_id": "toolu_1",
            "transcript_path": "",
        },
        tmp_path, daemon.path,
    )
    daemon.close()
    log = _hook_log(tmp_path)
    if not _have("jq"):
        assert "jq unavailable" in log, (
            "jq is absent, so the hook must say so in its log instead of "
            f"failing mutely. log: {log[:800]!r}"
        )
        assert not daemon.received, "delivered without jq — impossible"
        return
    assert len(daemon.received) >= 1, (
        "post-tool hook delivered nothing on a stock PATH — the daemon "
        f"received {len(daemon.received)} messages.\n"
        f"rc={proc.returncode} stderr={proc.stderr[:600]!r}\n"
        f"hook log: {log[:1200]!r}"
    )
    assert "dispatch STARTED" in log, (
        "the shell must log that it STARTED a dispatch, never that one "
        f"completed — it cannot know. log: {log[:800]!r}"
    )


def test_no_hook_depends_on_gnu_timeout():
    """`timeout` is coreutils, not POSIX, and is absent on stock macOS.

    Kept as a named check so the specific regression is legible in a failure
    report, even though the allowlist test below would also catch it.
    """
    offenders = []
    for path in sorted(HOOKS.glob("*.sh")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"(?<![\w.-])g?timeout\s+[\d.]", line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, (
        "hooks invoke GNU `timeout`, which stock macOS does not ship; the "
        "ceiling belongs inside the Python child (signal.setitimer):\n"
        + "\n".join(offenders)
    )


# Commands that exist on a typical Linux image but NOT on stock macOS (or the
# reverse). This is a denylist, not an allowlist, on purpose: an allowlist
# needs a shell parser to decide which word is in command position, and three
# iterations of that parser produced three different sets of false positives
# (a word inside a quoted log message; an argument left in command position
# once quotes were stripped). A denylist needs no parsing and cannot false-
# positive. Its honest limit: it catches a REPEAT of this class, not a
# first-of-its-kind command nobody has listed. Add a name when you learn of
# one -- that is cheaper than maintaining a parser that lies.
NON_PORTABLE_COMMANDS = {
    "timeout": "coreutils; absent on stock macOS — use signal.setitimer in the Python child",
    "gtimeout": "Homebrew coreutils only",
    "realpath": "absent on macOS before 12.3 — use python3 -c os.path.realpath",
    "sha256sum": "coreutils; macOS has shasum -a 256",
    "md5sum": "coreutils; macOS has md5",
    "nproc": "coreutils; macOS has sysctl -n hw.ncpu",
    "tac": "coreutils; macOS has tail -r",
    "seq": "present on macOS but not on all minimal Linux images",
    "readlink": "macOS readlink has no -f",
    "gsed": "Homebrew only",
    "gdate": "Homebrew only",
    "setsid": "util-linux; absent on macOS",
    "flock": "util-linux; absent on macOS",
    "xxd": "vim-common; not guaranteed",
    "pgrep": "present on both, but macOS pgrep lacks -c; flagged for review",
}


def test_hooks_avoid_commands_absent_on_a_target_platform():
    """Structural guard against the `timeout` class of bug.

    A command called bare is a hard dependency. A command called behind its own
    `command -v` probe is not, because the hook has a fallback for the host
    that lacks it -- `launchctl` is legitimately used that way, with a `nohup`
    fallback. That distinction is the whole difference between working code and
    the bug this file exists for, so the test encodes it.
    """
    offenders = []
    for path in sorted(HOOKS.glob("*.sh")):
        text = path.read_text()
        probed = set(re.findall(r"command -v\s+([A-Za-z_][A-Za-z0-9_.-]*)", text))
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for cmd, why in NON_PORTABLE_COMMANDS.items():
                if cmd in probed:
                    continue
                # Command position: start of line, or after a pipe/;/&&/$(.
                if re.search(r"(?:^|[|;&]|\$\()\s*" + cmd + r"\s", line):
                    offenders.append(f"{path.name}:{n}: {cmd} ({why})\n      {line.strip()}")
    assert not offenders, (
        "hooks call commands that are missing on a target platform and are not "
        "guarded by their own `command -v` probe:\n  " + "\n  ".join(offenders)
    )


def test_hooks_are_invoked_as_bash_everywhere():
    """The hooks are bash, and every invocation path must agree.

    They use `[[ ]]`, `&>`, and `<<<` — all bash, none POSIX. That is fine
    while they are run with bash, which hooks.json does. It stops being fine
    the moment any invocation switches to `sh`: on every Linux install
    /bin/sh is dash, and the breakage is a stderr line the user never sees.
    Pinned here because this test file itself got it wrong first.
    """
    manifest = json.loads((HOOKS / "hooks.json").read_text())
    commands = [
        hook["command"]
        for entries in manifest["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]
    assert commands, "no hook commands found in hooks.json"
    for cmd in commands:
        assert cmd.split()[0] == "bash", (
            f"hook invoked with {cmd.split()[0]!r}, not bash; the hooks use "
            f"bash-only syntax that dash rejects: {cmd!r}"
        )
    for path in sorted(HOOKS.glob("*.sh")):
        first = path.read_text().splitlines()[0]
        assert first == "#!/bin/bash", (
            f"{path.name} shebang is {first!r}; the hooks are bash scripts"
        )


def test_every_hook_is_valid_bash():
    """`bash -n` on each hook. Catches a syntax error before a user hits it."""
    for path in sorted(HOOKS.glob("*.sh")):
        proc = subprocess.run(["bash", "-n", str(path)], capture_output=True)
        assert proc.returncode == 0, (
            f"{path.name} is not valid bash: {proc.stderr.decode()[:400]}"
        )
