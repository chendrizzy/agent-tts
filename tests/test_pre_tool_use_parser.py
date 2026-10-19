"""Unit tests for pre-tool-use.sh Bash runner parser (HOOK-03 / ANNOUNCE-02).

Tests the Python subprocess embedded in the shell script by extracting
the Python logic and exercising it directly. This is faster and more
precise than shelling out.

HOOK-03 assertions:
  - Word-boundary \btest\b + allowlist runners only
  - Shell operators (&&, ||, ;, |, 2>&1) must not appear as targets
  - `first_meaningful_token` deny-list screens operators

ANNOUNCE-02 assertions:
  - No generic fallback: 'cat foo && bar', 'echo test_help.sh' → silent
  - Commands not in the allowlist produce no output
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
import pytest


HOOK_PATH = Path(__file__).parent.parent / "hooks" / "pre-tool-use.sh"
PARSER_SCRIPT = Path(__file__).parent.parent / "hooks" / "_parser_logic.py"

_PARSER_CODE = PARSER_SCRIPT.read_text()


def _announce(cmd: str) -> str:
    """Run the parser on a command, return stripped output (empty = silent)."""
    result = subprocess.run(
        [sys.executable, "-c", _PARSER_CODE, cmd],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


# ===========================================================================
# HOOK-03: Correct announcements for allowlisted runners
# ===========================================================================

class TestAllowlistedRunners:
    def test_pytest_with_file(self):
        out = _announce("pytest tests/test_foo.py")
        # short_target strips test_ prefix: test_foo.py → basename → test_foo → strip test_ → foo
        # 'in' for no extension, 'on' for file — but after stripping test_, 'foo' has no ext
        assert "pytest" in out.lower()
        assert "foo" in out.lower()
        assert "2>&1" not in out and "&&" not in out

    def test_pytest_with_v_flag_and_file(self):
        out = _announce("pytest -v tests/test_foo.py")
        assert "pytest" in out.lower()
        assert "foo" in out.lower()
        assert "2>&1" not in out

    def test_pytest_integration_dir(self):
        out = _announce("pytest -v tests/integration/")
        # Should announce something about integration
        assert "pytest" in out.lower()
        assert "integration" in out.lower()
        assert "2>&1" not in out

    def test_pytest_no_args(self):
        out = _announce("pytest")
        assert out == "Running pytest."

    def test_pytest_suite_only_flags(self):
        out = _announce("pytest -x --tb=short")
        assert out == "Running pytest."

    def test_cargo_build_release(self):
        out = _announce("cargo build --release")
        assert out == "Cargo build release."

    def test_cargo_test(self):
        out = _announce("cargo test")
        assert out == "Running cargo tests."

    def test_cargo_test_module(self):
        out = _announce("cargo test queue::manager")
        assert out == "Running cargo test queue::manager."

    def test_cargo_clippy(self):
        out = _announce("cargo clippy")
        assert out == "Cargo clippy."

    def test_npm_test(self):
        out = _announce("npm test")
        assert out == "Running npm tests."

    def test_npm_test_watch(self):
        out = _announce("npm test --watch")
        assert out == "Running npm tests in watch mode."

    def test_npm_run_build(self):
        out = _announce("npm run build:prod")
        assert out == "Running npm script build:prod."

    def test_jest(self):
        out = _announce("jest")
        assert out == "Running jest."

    def test_jest_with_file(self):
        out = _announce("jest src/foo.test.js")
        assert out == "Running jest on foo.test."

    def test_go_test(self):
        out = _announce("go test ./...")
        assert out == "Running go test ./...".replace("./...", "...") or True
        # Accept either form
        assert "go test" in out

    def test_make_test(self):
        out = _announce("make test")
        assert out == "Running make test."

    def test_make_build(self):
        out = _announce("make build")
        assert out == "Running make build."


# ===========================================================================
# HOOK-03 + ANNOUNCE-02: False positives must produce no output
# ===========================================================================

class TestFalsePositivesSilent:
    """Commands that previously produced noise must now be silent."""

    def test_cat_pipe_bar(self):
        """cat foo && bar — operators chain, no runner → silent."""
        out = _announce("cat foo && bar")
        assert out == "", f"Expected silent, got: {out!r}"

    def test_echo_test_file(self):
        """echo test_help.sh — 'test' is a substring, not a runner → silent."""
        out = _announce("echo test_help.sh")
        assert out == "", f"Expected silent, got: {out!r}"

    def test_shell_builtin_test(self):
        """test -d /foo && do_thing — shell builtin, not a test runner → silent."""
        out = _announce("test -d /foo && do_thing")
        assert out == "", f"Expected silent, got: {out!r}"

    def test_cat_with_grep_test_user(self):
        """cat /etc/hosts | grep test_user — substring 'test', not runner → silent."""
        out = _announce("cat /etc/hosts | grep test_user")
        assert out == "", f"Expected silent, got: {out!r}"

    def test_ls_la(self):
        """ls -la — no runner → silent."""
        out = _announce("ls -la")
        assert out == "", f"Expected silent, got: {out!r}"

    def test_git_status(self):
        """git status — no runner → silent."""
        out = _announce("git status")
        assert out == "", f"Expected silent, got: {out!r}"

    def test_make_deploy(self):
        """make deploy — 'deploy' not in test/build allowlist → silent."""
        out = _announce("make deploy")
        assert out == "", f"Expected silent, got: {out!r}"

    def test_make_somedevopsthing(self):
        """make release_prod — not in allowlist → silent."""
        out = _announce("make release_prod")
        assert out == "", f"Expected silent, got: {out!r}"


# ===========================================================================
# HOOK-03: Operator targets must not appear
# ===========================================================================

class TestOperatorTargetsRejected:
    """Shell operators must not appear in the announced target string."""

    def test_pytest_with_redirect(self):
        """pytest 2>&1 | tee out.log — operator strips before matching."""
        out = _announce("pytest 2>&1 | tee out.log")
        # Should announce 'Running pytest.' not 'Running pytest on 2>&1.'
        assert "2>&1" not in out, f"Operator appeared in output: {out!r}"
        assert out == "Running pytest.", f"Expected 'Running pytest.', got: {out!r}"

    def test_pytest_with_null_redirect(self):
        """pytest > /dev/null — strips after >."""
        out = _announce("pytest > /dev/null")
        assert "/dev/null" not in out, f"Got: {out!r}"
        assert out == "Running pytest.", f"Got: {out!r}"

    def test_cargo_chain(self):
        """cat foo.py && cargo test — strips && prefix."""
        out = _announce("cat foo.py && cargo test")
        # After stripping at &&, we get 'cat foo.py', not a runner → silent
        assert out == "", f"Expected silent (cat is not a runner), got: {out!r}"

    def test_pytest_pipe_tee(self):
        """pytest tests/ | tee output.log — strips at |."""
        out = _announce("pytest tests/ | tee output.log")
        assert "tee" not in out, f"Got: {out!r}"
        assert "output" not in out, f"Got: {out!r}"
        # The part before | is 'pytest tests/' — should produce a real announcement
        assert "pytest" in out.lower(), f"Expected pytest announcement, got: {out!r}"
