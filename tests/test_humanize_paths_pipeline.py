"""Integration test: path shortening must survive the rest of the clean pass.

Verifies that verbose filesystem paths do NOT reach TTS verbatim — i.e., that
the pipeline path (ProcessStage._clean_text_sync) actually shortens paths,
eliminating 'slash backend slash app slash...' readouts.

HISTORY — read before touching the step order in _clean_text_sync.
Path shortening ran AFTER markdown normalization from v0.1.0 through v0.1.11.
Normalization rewrites the punctuation inside a path (an extension's "." becomes
the word "dot"), so by the time the shortener ran it no longer saw a path, and:

  "The file /Users/alice/dev/myapp/src/types.py is ready."
      was spoken as  "The file .py is ready."          <- filename DELETED
  "Updated /Users/alice/dev/myapp/src/content_router.py just now."
      was spoken as  "Updated .claude/tts/... dot py"  <- full chain READ OUT

Two of the tests below were red for three months (first recorded 2026-07-04) and
nobody noticed, because this file was not in `make verify`. It is now. The two
symptoms are pinned verbatim in test_measured_symptoms_stay_fixed, and the step
order itself is pinned in test_shortening_runs_before_markdown_normalization —
the behavioural tests alone would not say WHY they broke.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import sys
from pathlib import Path

import pytest  # noqa: F401  (collected via pytest; kept for parity with suite)

sys.path.insert(0, str(Path(__file__).parent.parent))

from daemon.pipeline.process_stage import ProcessStage
from daemon.pipeline.ingest_stage import IngestMessage


def _make_ingest(text: str) -> IngestMessage:
    import time
    return IngestMessage(
        content=text,
        session_id="test",
        priority=5,
        request_id="test-req-1",
        ingested_at=time.time(),
    )


class TestHumanizePathsInPipeline:
    """ProcessStage._clean_text_sync must reduce verbose paths."""

    def _clean(self, text: str) -> str:
        stage = ProcessStage()
        return stage._clean_text_sync(text)

    def test_absolute_path_humanized(self):
        text = "The file /Volumes/BOLT/Users/justinchen/.claude/tts/daemon/types.py is ready."
        result = self._clean(text)
        # Should NOT contain the long /Volumes/... prefix
        assert "/Volumes/BOLT" not in result, (
            f"Long path prefix not humanized: {result!r}"
        )
        # Should contain a shortened form
        assert "types" in result.lower() or "daemon" in result.lower(), (
            f"Expected shortened path reference, got: {result!r}"
        )

    def test_relative_path_humanized(self):
        text = "Check ./daemon/content_router.py for the classifier."
        result = self._clean(text)
        # Relative path should be shortened to 'content_router.py' or 'daemon/content_router.py'
        assert "content_router" in result, (
            f"Expected content_router in result: {result!r}"
        )

    def test_home_path_humanized(self):
        text = "Config at ~/projects/web/api/server.ts is loaded."
        result = self._clean(text)
        assert "~/projects/web/api" not in result, (
            f"Home path not humanized: {result!r}"
        )
        # Should still reference server.ts or api/server.ts
        assert "server" in result, f"Expected server in result: {result!r}"

    def test_plain_text_unchanged(self):
        """Text without paths should pass through unchanged."""
        text = "All 91 tests passed in 0.22 seconds."
        result = self._clean(text)
        assert "91 tests passed" in result, (
            f"Plain text was unexpectedly modified: {result!r}"
        )

    def test_process_stage_integration(self):
        """Full async process() call applies humanize_paths.

        Asserts the DIRECTORY CHAIN is gone, not merely that the user's home
        prefix is. The weaker version of this assertion passed for three months
        while the pipeline spoke every intermediate directory aloud: dropping
        "/Users/<name>" and keeping ".claude/tts/daemon/" satisfied it.
        """
        text = "/Users/justinchen/.claude/tts/daemon/content_router.py was modified."
        stage = ProcessStage()
        msg = _make_ingest(text)
        result = asyncio.run(stage.process(msg))
        assert "/Users/justinchen" not in result.cleaned, (
            f"Full path not humanized in pipeline: {result.cleaned!r}"
        )
        assert "content_router" in result.cleaned, (
            f"Expected content_router in cleaned output: {result.cleaned!r}"
        )
        assert ".claude" not in result.cleaned and "/tts/" not in result.cleaned, (
            "intermediate directories survived — the listener hears the whole "
            f"chain read out, which is what shortening exists to prevent: "
            f"{result.cleaned!r}"
        )


class TestOrderingRegression:
    """The specific defect that shipped in v0.1.0–v0.1.11, pinned two ways."""

    def _clean(self, text: str) -> str:
        return ProcessStage()._clean_text_sync(text)

    def test_measured_symptoms_stay_fixed(self):
        """Both measured symptoms, as a listener would hear them.

        Deliberately uses a path belonging to nobody, so this test says the same
        thing on any machine and in the public fork.
        """
        short_stem = self._clean(
            "The file /Users/alice/dev/myapp/src/types.py is ready."
        )
        # Symptom 1: the filename was deleted outright, leaving ". .py".
        assert "types" in short_stem, (
            "the filename was dropped from spoken output — this is the "
            f"'The file .py is ready.' defect returning: {short_stem!r}"
        )
        assert not short_stem.startswith("The file ."), (
            f"stray leading period before the extension: {short_stem!r}"
        )

        long_stem = self._clean(
            "Updated /Users/alice/dev/myapp/src/content_router.py just now."
        )
        # Symptom 2: the whole directory chain survived and was read aloud.
        assert "content_router" in long_stem, f"filename lost: {long_stem!r}"
        for chain in ("dev", "myapp", "Users", "alice"):
            assert chain not in long_stem, (
                f"intermediate directory {chain!r} survived and would be "
                f"spoken: {long_stem!r}"
            )

    def test_shortening_runs_before_markdown_normalization(self):
        """Pin the step ORDER in the source, not just its effect.

        The behavioural tests above would go red if the order flipped back, but
        they would not say why. This one names the cause at the callsite, so
        whoever flips it reads the reason instead of rediscovering it.
        """
        src = inspect.getsource(ProcessStage._clean_text_sync)
        tree = ast.parse(ast.unparse(ast.parse(src.strip())))

        order = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("humanize_paths", "normalize_for_speech"):
                    order.append((node.lineno, node.func.id))
        calls = [name for _, name in sorted(order)]

        assert "humanize_paths" in calls, (
            "_clean_text_sync no longer calls humanize_paths at all; verbose "
            "paths will be read out directory by directory"
        )
        assert "normalize_for_speech" in calls, (
            "_clean_text_sync no longer calls normalize_for_speech"
        )
        assert calls.index("humanize_paths") < calls.index("normalize_for_speech"), (
            "humanize_paths must run BEFORE normalize_for_speech. Normalization "
            "rewrites the punctuation inside a path (an extension's '.' becomes "
            "the word 'dot'), after which the path matcher sees no path and "
            "either deletes the filename or leaves the whole directory chain to "
            "be spoken. That shipped in v0.1.0 through v0.1.11. "
            f"Observed call order: {calls}"
        )

    def test_clean_pass_is_idempotent(self):
        """Running the pass twice must equal running it once.

        The pass is shared by every speech-bound route and is documented as
        idempotent; reordering its steps is exactly the kind of change that can
        break that quietly.
        """
        for text in (
            "The file /Users/alice/dev/myapp/src/types.py is ready.",
            "Config at ~/projects/web/api/server.ts is loaded.",
            "Check ./daemon/content_router.py for the classifier.",
            "All 91 tests passed in 0.22 seconds.",
            "He said it is 3/4 done and/or nearly there.",
            "",
            "....",
        ):
            once = self._clean(text)
            twice = self._clean(once)
            assert once == twice, (
                f"not idempotent for {text!r}: once={once!r} twice={twice!r}"
            )

    def test_prose_slashes_are_not_treated_as_paths(self):
        """Shortening must not reach into ordinary prose.

        Running the path matcher first makes this the relevant risk: a matcher
        that grabs "and/or" or "3/4" would mangle prose that was previously
        safe. Pinned so a future widening of the matcher trips here.
        """
        result = self._clean("He said it is 3/4 done and/or nearly there.")
        assert "3/4" in result, f"numeric fraction mangled: {result!r}"
        assert "and/or" in result, f"prose slash mangled: {result!r}"
