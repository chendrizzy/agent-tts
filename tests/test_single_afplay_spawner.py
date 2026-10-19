"""AUDIO-02: exactly one function in daemon/ may start an audio player.

WHY THE INVARIANT EXISTS
Two spawn paths would mean two audio paths with no shared serialization, so two
clips could play over each other; and two separate copies of the hang watchdog,
the cancel path, and the file-descriptor cleanup. It is enforced by deletion —
the legacy sites were removed — and this file exists only to fail loudly if a
new one is added back.

WHY THIS FILE WAS REWRITTEN (2026-10-11)
The original check counted sites by text-matching the player program's name as a
literal first argument to a spawn call. When the volume flag was wired in, the
name moved out of the call and into a list that is expanded into it, and the
count silently went from 1 to 0. It had been failing since 2026-07-04 and nobody
noticed, because this file was not in `make verify`. It is now.

Note the two separate harms, because the second is the worse one: it reported
the wrong number, AND it could no longer tell "fine" from "violated" in either
direction — a genuinely added second site would not have made it pass either.

So this version inverts the question. It does not ask "where does the player's
name appear" (a question about argument shape, which is what rotted). It asks
"where does this source call a process-creating API" — a property of the callee,
immune to argument shape, to a fourth platform, and to the name being computed.
It then pins the ENTIRE spawn census rather than trying to decide by dataflow
which spawns are audio players. Deciding that is the problem that defeated the
first implementation.

DO NOT reintroduce matching on player program names.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Dict, List, Set, Tuple

DAEMON_DIR = Path(__file__).parent.parent / "daemon"

# Process-creating APIs. Keyed on the callee, never on the arguments.
SPAWN_ATTRS: Set[Tuple[str, str]] = {
    ("subprocess", "Popen"),
    ("subprocess", "run"),
    ("subprocess", "call"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
    ("asyncio", "create_subprocess_exec"),
    ("asyncio", "create_subprocess_shell"),
    ("os", "system"),
    ("os", "posix_spawn"),
    ("os", "posix_spawnp"),
    ("pty", "spawn"),
}
# Bare names that are spawns regardless of module (covers `from x import y`).
SPAWN_NAMES: Set[str] = {
    "Popen", "check_call", "check_output", "posix_spawn", "posix_spawnp",
    "create_subprocess_exec", "create_subprocess_shell",
}
# os.exec*/os.spawn* are matched by prefix.
SPAWN_PREFIXES: Tuple[str, ...] = ("exec", "spawn")

# ---------------------------------------------------------------------------
# THE CENSUS.  Every (file, function) in daemon/ that creates a process.
#
# To edit this list you must write a reason, and `audio` must be honest: `True`
# means this site STARTS an audio player.  Exactly one entry may say True.
#
# If a check below fails, do ONE of these — never widen a pattern:
#   (a) You moved/renamed/refactored the audio site: update its entry, and
#       confirm the watchdog, the cross-session lock, and the fd cleanup still
#       wrap the new location.
#   (b) You added a new spawn: route it through the existing audio site. Only
#       if it does not start a player (a cleanup, a non-audio tool) add an
#       entry here with audio=False and a reason.
# ---------------------------------------------------------------------------
# NOTE: the private source tree (tts) pins a DIFFERENT census. There, playback
# spawns inline inside _play_audio_inner with one branch per platform; here it
# is routed through the Platform seam, so the one audio site is
# platforms/base.py::spawn_player and _play_audio_inner spawns nothing itself.
# Both trees satisfy the same invariant at different locations; do not "sync"
# one census onto the other.
CENSUS: Dict[Tuple[str, str], dict] = {
    # --- the one audio-player spawner ------------------------------------
    ("platforms/base.py", "spawn_player"): {
        "audio": True,
        "calls": 1,
        "reason": "the canonical audio spawn site: _play_audio_inner delegates "
                  "here through the Platform seam, and the per-OS argv comes "
                  "from build_player_cmd, which is pure and spawns nothing",
    },
    # --- everything else that creates a process, and why it is not audio --
    ("engines/system_tts_engine.py", "synthesize"): {
        "audio": False, "calls": 1,
        "reason": "`say --file-format=WAVE -o <file>` or `espeak -w <file>`: "
                  "SYNTHESIZES speech to a file and never plays it. The player "
                  "that plays that file is the one audio site above.",
    },
    ("ollama_integration.py", "_detect_gpu"): {
        "audio": False, "calls": 2,
        "reason": "nvidia-smi and system_profiler, to detect a GPU",
    },
    ("ollama_integration.py", "is_installed"): {
        "audio": False, "calls": 1,
        "reason": "`ollama --version`, to probe for the local LLM runtime",
    },
    ("ollama_integration.py", "start_service"): {
        "audio": False, "calls": 1, "reason": "starts `ollama serve`",
    },
    ("pipeline/generate_stage.py", "_signal_disk_full"): {
        "audio": False, "calls": 2,
        "reason": "osascript / notify-send desktop alert when the disk is full; "
                  "a visual notification, not audio",
    },
    ("pipeline/generate_stage.py", "_get_audio_duration"): {
        "audio": False, "calls": 1,
        "reason": "ffprobe, to read a clip's duration; reads the file, plays nothing",
    },
    ("pipeline/kokoro_engine.py", "_spawn"): {
        "audio": False, "calls": 1,
        "reason": "starts the persistent Kokoro synth worker (a Python process); "
                  "it SYNTHESIZES to a file and never plays it",
    },
    ("service_ctl.py", "_run"): {
        "audio": False, "calls": 1,
        "reason": "launchctl/systemctl/ps argv lists, to find, stop and start the daemon service",
    },
    ("service_ctl.py", "_spawn"): {
        "audio": False, "calls": 1,
        "reason": "starts the daemon itself (detached) when no service manager owns it",
    },
    ("platforms/base.py", "install_service"): {
        "audio": False, "calls": 6,
        "reason": "launchctl bootout/bootstrap/kickstart on macOS and "
                  "systemctl/loginctl on Linux, to install the background service",
    },
    ("platforms/base.py", "uninstall_service"): {
        "audio": False, "calls": 3,
        "reason": "launchctl bootout and systemctl disable/daemon-reload, to "
                  "remove the background service",
    },
    ("session_control.py", "_identify_uncached"): {
        "audio": False, "calls": 1,
        "reason": "git rev-parse to identify a session's repo/worktree/branch "
                  "for voice-override rules; never starts a player",
    },
    ("tts_daemon.py", "_compute_build_stamp"): {
        "audio": False, "calls": 2,
        "reason": "git rev-parse and git status, for the build stamp",
    },
    ("tts_daemon.py", "_cleanup_background_processes"): {
        "audio": False, "calls": 2,
        "reason": "pkill of edge-tts and afplay leftovers from an earlier crash; "
                  "kills players, never starts one",
    },
}

CENSUS_FILE = Path(__file__).name


def collect_daemon_py_files() -> List[Path]:
    """Return all .py files under daemon/ excluding __pycache__."""
    files = []
    for root, dirs, fnames in os.walk(DAEMON_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in fnames:
            if fname.endswith(".py"):
                files.append(Path(root) / fname)
    return sorted(files)


def _is_spawn_callee(node: ast.AST) -> bool:
    """True if this Call's callee is a process-creating API.

    Comments, docstrings and log strings cannot reach here: they are not Call
    nodes. That is why this needs no exclusion list, which is the thing that
    would otherwise grow forever and eventually hide a real hit.
    """
    if isinstance(node, ast.Attribute):
        attr = node.attr
        mod = node.value.id if isinstance(node.value, ast.Name) else ""
        if (mod, attr) in SPAWN_ATTRS:
            return True
        if mod == "os" and attr.startswith(SPAWN_PREFIXES):
            return True
        return attr in SPAWN_NAMES
    if isinstance(node, ast.Name):
        return node.id in SPAWN_NAMES
    return False


def scan_source(source: str) -> List[Tuple[str, int]]:
    """Return (enclosing function name, lineno) for every spawn call.

    The unit is the enclosing FUNCTION, not the call and not the file. A call is
    too fine — the one legitimate site holds three, one per platform, so a
    per-call count drifts for innocent reasons and says nothing about
    uniqueness. A file is too coarse — a second spawn function in the same
    module is a real violation and a file-level count would pass it. A function
    is the region that can own the lock, the watchdog, the cancel path and the
    fd cleanup, and it survives being renamed or moved.
    """
    tree = ast.parse(source)
    enclosing: Dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                enclosing[id(child)] = node.name

    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_spawn_callee(node.func):
            hits.append((enclosing.get(id(node), "<module>"), node.lineno))
    return hits


def scan_tree() -> Dict[Tuple[str, str], List[int]]:
    """Scan daemon/ and return {(relpath, function): [linenos]}."""
    found: Dict[Tuple[str, str], List[int]] = {}
    for path in collect_daemon_py_files():
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(DAEMON_DIR))
        for func, lineno in scan_source(source):
            found.setdefault((rel, func), []).append(lineno)
    return found


# --- the scanner's own self-test -------------------------------------------
# This is the only thing that would have caught the original rot, so it runs
# before any assertion about the real tree.

_GOOD_SRC = '''
import asyncio, subprocess
# afplay is mentioned in this comment and must not be counted
async def _play_audio_inner(self, path):
    """Spawns afplay on macOS. This docstring must not be counted."""
    logger.info("about to spawn afplay for %s", path)
    if self._platform == "darwin":
        cmd = ["afplay"]                      # name assembled into a list
        if self.volume != 1.0:
            cmd += ["-v", "1.0"]
        cmd.append(path)
        proc = await asyncio.create_subprocess_exec(*cmd)
    elif self._platform == "linux":
        proc = await asyncio.create_subprocess_exec("mpv", path)
    else:
        proc = await asyncio.create_subprocess_exec("ffplay", path)
    return proc

def _cleanup(self):
    subprocess.run(["pkill", "-f", "afplay.*"])
'''

_BAD_SRC = _GOOD_SRC + '''
async def _sneaky_retry(self, path):
    # a SECOND audio path, which is exactly what this guard must catch
    return await asyncio.create_subprocess_exec("afplay", path)
'''


def test_scanner_self_test():
    """Scan two embedded sources so a broken scanner cannot pass silently.

    The original check died by reporting zero. A tripwire whose natural failure
    direction is silence is not a tripwire, so the scanner is scanned.
    """
    good = scan_source(_GOOD_SRC)
    good_funcs = sorted({f for f, _ in good})
    assert good_funcs == ["_cleanup", "_play_audio_inner"], (
        f"scanner mis-reads the known-good source: found {good_funcs!r}. "
        "It must find exactly the two spawning functions — and must NOT be "
        "fooled by the player name in a comment, a docstring, a log string, "
        "or the pkill pattern."
    )
    play_calls = [ln for f, ln in good if f == "_play_audio_inner"]
    assert len(play_calls) == 3, (
        f"expected 3 platform-branch spawn calls in the known-good site, "
        f"found {len(play_calls)} — the scanner is missing the branch whose "
        "program name is assembled into a list, which is the exact shape that "
        "broke the original check"
    )

    bad_funcs = sorted({f for f, _ in scan_source(_BAD_SRC)})
    assert bad_funcs == ["_cleanup", "_play_audio_inner", "_sneaky_retry"], (
        f"scanner FAILS to see an added second spawn site: {bad_funcs!r}. "
        "A check that cannot detect a violation is worse than no check."
    )


def test_scanner_is_not_silently_broken():
    """Zero spawn calls across the whole daemon means the scanner is broken."""
    files = collect_daemon_py_files()
    assert files, f"No Python files found under {DAEMON_DIR} — check the path"
    found = scan_tree()
    assert found, (
        f"Scanned {len(files)} files under daemon/ and found 0 process-spawn "
        "calls. This means THE SCANNER IS BROKEN, not that the code is clean — "
        "the daemon demonstrably spawns an audio player. Fix the scanner; see "
        f"test_scanner_self_test in {CENSUS_FILE} for which half failed."
    )


def _format(found: Dict[Tuple[str, str], List[int]]) -> str:
    lines = []
    for key in sorted(set(found) | set(CENSUS)):
        rel, func = key
        linenos = found.get(key)
        if linenos is None:
            tag = "MISSING (pinned, not found)"
            where = "-"
        else:
            tag = "known" if key in CENSUS else "NEW"
            where = ",".join(str(n) for n in linenos)
        lines.append(f"  [{tag:28s}] {rel}:{where} in {func}()")
    return "\n".join(lines)


_RULE = (
    "RULE: exactly one function in daemon/ may start an audio player.\n"
    "WHY:  two spawn paths means no shared serialization (clips overlap) and "
    "two copies of the hang watchdog, the cancel path and fd cleanup.\n"
    f"The pinned census is CENSUS in tests/{CENSUS_FILE}; each entry needs "
    "path, function, calls, audio, reason.\n"
    "Resolve by (a) updating a moved site's entry — then confirm the watchdog, "
    "the cross-session lock and fd cleanup still wrap it — or (b) routing a new "
    "spawn through the existing audio site; add an audio=False entry only if it "
    "does not start a player.\n"
    "HISTORY: this check silently reported 0 from 2026-07-04 to 2026-10-11 "
    "because it matched player program names as literal arguments. Do not "
    "reintroduce name matching.\n"
    "This is a UNIQUENESS tripwire only. It does not verify that the watchdog, "
    "cancel, or fd cleanup are correct."
)


def test_spawn_census_matches():
    """The set of spawning (file, function) pairs must equal the pinned census."""
    found = scan_tree()
    assert set(found) == set(CENSUS), (
        f"{_RULE}\n\nSpawn sites in daemon/ differ from the pinned census:\n"
        f"{_format(found)}"
    )


def test_each_site_spawn_call_count_is_pinned():
    """Catch a second spawn call smuggled into an already-pinned function."""
    found = scan_tree()
    for key, entry in CENSUS.items():
        linenos = found.get(key, [])
        assert len(linenos) == entry["calls"], (
            f"{_RULE}\n\n{key[0]} in {key[1]}() holds {len(linenos)} spawn "
            f"calls (lines {linenos}), census pins {entry['calls']}. If you "
            "added a platform branch, update the count. If you added a second "
            "playback path inside this function, it bypasses the queue and the "
            "lock — route it through the existing one."
        )


def test_exactly_one_audio_spawn_site():
    """The invariant itself: one audio player spawner, no more."""
    audio = [k for k, v in CENSUS.items() if v["audio"]]
    assert len(audio) == 1, (
        f"{_RULE}\n\nCensus declares {len(audio)} audio spawn sites: {audio}"
    )
    found = scan_tree()
    assert audio[0] in found, (
        f"{_RULE}\n\nThe one declared audio spawn site {audio[0]} was not "
        f"found in the source at all:\n{_format(found)}"
    )


def test_census_entries_carry_a_reason():
    """An entry without a reason is a silent exemption."""
    for key, entry in CENSUS.items():
        assert entry.get("reason", "").strip(), (
            f"census entry {key} has no reason; a future reader cannot tell "
            "whether it is legitimate"
        )
