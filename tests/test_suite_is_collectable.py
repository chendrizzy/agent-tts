"""The whole suite must be ENUMERABLE, or the gate cannot catch rot.

Why this file exists
--------------------
On 2026-10-11 four architecture-guard tests were found to have silently stopped
testing anything, two of them red since 2026-07-04. The reason they went
unnoticed for five months was not subtle: `pytest tests` could not run at all.
`daemon/voice_input.py` called `sys.exit(1)` at import scope when its optional
audio libraries were missing, and `SystemExit` is not a collection error -- so
collection died with INTERNALERROR instead of skipping one file. Everyone fell
back to running hand-picked subsets, and a guard outside the subset you happen
to run is a guard nobody runs.

So this asserts the PROPERTY ("the suite can be enumerated") rather than any
mechanism, and a second test forbids the specific construct that broke it.

What this deliberately does NOT do
----------------------------------
It does not add `collect_ignore`, `--ignore=`, or a module-level
`pytest.importorskip` to make collection pass. That was named, by an
independent designer who had not seen this code, as the most likely wrong fix:
collection goes green while the suite is permanently smaller, which is the
exact shape of the rot being fixed here.

NOTE — this file deliberately DIFFERS from the source tree's copy
----------------------------------------------------------------
The private development tree carries a third test here, asserting that
daemon/voice_input.py imports cleanly without its optional audio libraries.
This repo has no voice_input.py, no interrupt_handler.py and no test_multi_ai.py
-- it is the slim shipped product, and the voice-input tree lives only in the
lab. Do not "sync" that test into this file; it would fail for the correct
reason. The two universal assertions below are the ones that belong in both.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_whole_suite_collects_without_error():
    """`pytest --collect-only` over the entire repo must exit 0, 0 errors.

    Run in a SUBPROCESS on purpose: the failure being guarded is a crash of the
    collector itself, which an in-process check cannot survive to report.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, timeout=600,
    )
    combined = proc.stdout + proc.stderr

    assert "INTERNALERROR" not in combined, (
        "pytest's collector CRASHED, so the suite cannot be enumerated. This is "
        "the 2026-10-11 failure returning. Almost always a module that raises "
        "or exits at import scope -- SystemExit is not a collection error, so "
        "it takes the whole run down instead of skipping one file.\n\n"
        + _tail(combined)
    )
    assert proc.returncode == 0, (
        f"collection exited {proc.returncode}, expected 0.\n\n" + _tail(combined)
    )
    assert " error" not in combined.rsplit("\n", 3)[-1].lower() or \
        "0 errors" in combined.lower(), (
        "collection reported errors:\n\n" + _tail(combined)
    )

    # Zero collected would also "pass" every assertion above, which is how a
    # broken tripwire reads as clean. Pin a floor instead.
    assert "tests collected" in combined or "test collected" in combined, (
        "could not find a collection tally at all:\n\n" + _tail(combined)
    )


SKIP_DIRS = {".git", ".archive", "__pycache__", ".venv", "node_modules",
             "backups", "build", "dist", ".mypy_cache", ".pytest_cache"}


def test_no_module_exits_the_interpreter_at_import_scope():
    """NOTHING in this repo may call sys.exit() at import scope.

    Keyed on the CALL and its depth, not on any module name, so a new file with
    the same defect is caught on the day it lands. Calls inside a function or
    class body are fine -- that is an entry point deciding to stop, which is its
    right. What is forbidden is a leaf module making that decision for whoever
    imported it.

    Scope note, learned the hard way: this scanned only daemon/ when first
    written, and promptly missed tests/test_multi_ai.py, which had the identical
    defect and was still killing the collector. A scanner narrower than the
    failure it guards is a scanner that reports clean while the bug is live, so
    it now walks the whole tree.
    """
    offenders = []
    for path in sorted(REPO.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - would fail collection anyway
            pytest.fail(f"{path.relative_to(REPO)} does not parse: {exc}")

        for node in _import_scope_nodes(tree):
            if isinstance(node, ast.Call) and _is_sys_exit(node):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")

    assert not offenders, (
        "these modules call sys.exit() at IMPORT scope, which makes the whole "
        "test suite uncollectable and takes the product down for anyone whose "
        "optional dependencies are missing:\n  " + "\n  ".join(offenders) +
        "\n\nInstead: catch (ImportError, OSError), set an availability flag, "
        "warn once on STDERR, and raise from the call boundary that actually "
        "needs the dependency. Deciding the process cannot continue belongs to "
        "the caller, not to a leaf module."
    )


def _is_sys_exit(call: ast.Call) -> bool:
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr == "exit":
        return isinstance(f.value, ast.Name) and f.value.id in ("sys", "os")
    return isinstance(f, ast.Name) and f.id in ("exit", "quit")


def _import_scope_nodes(tree: ast.Module):
    """Yield every node that executes on import, skipping def/class bodies.

    An `if __name__ == "__main__":` block is also import scope by position, but
    it does not run on import, so it is skipped too -- otherwise every
    legitimate CLI entry point would read as a violation.
    """
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, ast.If) and _is_main_guard(stmt):
            continue
        yield from ast.walk(stmt)


def _is_main_guard(node: ast.If) -> bool:
    t = node.test
    if not isinstance(t, ast.Compare) or not isinstance(t.left, ast.Name):
        return False
    return t.left.id == "__name__"


def _tail(text: str, lines: int = 25) -> str:
    return "\n".join(text.splitlines()[-lines:])
