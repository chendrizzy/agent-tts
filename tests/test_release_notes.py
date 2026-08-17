"""Gate for the CHANGELOG-driven release notes (scripts/release_notes.py).

The last test is the important one: it fails the build when the version in
pyproject.toml has no CHANGELOG section, so a release can never be cut with
empty or wrong notes — the defect that shipped v0.1.7's notes as "a dead-code
PR". Part of `make verify`.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_manifests as _bm  # noqa: E402
from release_notes import extract_release_notes  # noqa: E402

SAMPLE = """# Changelog

## [Unreleased]

## [0.2.0] — 2026-08-16

### Added
- a thing

### Fixed
- another thing

## [0.1.9] — 2026-01-01

### Added
- older thing
"""


def test_extracts_the_named_section_only():
    notes = extract_release_notes(SAMPLE, "0.2.0")
    assert "a thing" in notes and "another thing" in notes
    assert "older thing" not in notes, "leaked into the previous release's section"
    assert not notes.startswith("## "), "heading should be stripped"


def test_last_section_runs_to_eof():
    assert "older thing" in extract_release_notes(SAMPLE, "0.1.9")


def test_missing_version_raises():
    with pytest.raises(ValueError, match="no '## \\[9.9.9\\]' section"):
        extract_release_notes(SAMPLE, "9.9.9")


def test_empty_section_raises():
    """An empty [Unreleased] must not become the release body."""
    with pytest.raises(ValueError, match="is empty"):
        extract_release_notes(SAMPLE, "Unreleased")


def test_version_prefix_is_not_a_partial_match():
    """0.1.9 must not match a '## [0.1.90]' heading."""
    with pytest.raises(ValueError):
        extract_release_notes("# Changelog\n\n## [0.1.90]\n\n- x\n", "0.1.9")


def test_current_version_has_release_notes():
    """RELEASE GATE: the shipped version must have a CHANGELOG section."""
    version = _bm.pyproject_version((ROOT / "pyproject.toml").read_text())
    notes = extract_release_notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
                                  version)
    assert len(notes) > 40, f"v{version} release notes look like a stub: {notes!r}"
