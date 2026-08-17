#!/usr/bin/env python3
"""Extract one version's section from CHANGELOG.md for `gh release create`.

`release.yml` used `--generate-notes`, which lists merged PRs since the last
tag. This repo releases straight from `main` without PRs, so those notes
described whatever PR happened to be in range instead of the release itself
(v0.1.7 shipped notes about a dead-code PR). The CHANGELOG is the real record,
so the release quotes it.

Pure function + thin CLI, matching scripts/build_manifests.py. Exits non-zero
when the version has no CHANGELOG section, so a release cannot be cut without
release notes.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"


def extract_release_notes(changelog: str, version: str) -> str:
    """Return the body under `## [version]`, stripped of its heading.

    Stops at the next `## ` heading (or EOF). Raises ValueError if the section
    is missing or holds no content.
    """
    heading = re.compile(rf"^## \[{re.escape(version)}\](?:\s|$)")
    lines = changelog.splitlines()
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise ValueError(f"CHANGELOG.md has no '## [{version}]' section")

    body: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        body.append(line)

    notes = "\n".join(body).strip()
    if not notes:
        raise ValueError(f"CHANGELOG.md section '## [{version}]' is empty")
    return notes


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <version>", file=sys.stderr)
        return 2
    try:
        print(extract_release_notes(CHANGELOG.read_text(encoding="utf-8"), argv[1]))
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
