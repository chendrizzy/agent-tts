"""Tests for the deterministic substance-preserving selector (STORY G002).

Covers:
  * HARD GATE — the motivating record 6a466f1f: the "…constant back and forth…"
    blockquote substance MUST survive selection (the exact bug the qwen path
    dropped). The record is bundled byte-exact from capture.jsonl
    (tests/fixtures/speech_selection_6a466f1f.json) so the test is deterministic;
    when the live capture file is present it is cross-checked for drift.
  * Golden fixture (b) — a plain multi-paragraph reply (the majority input class)
    is NOT reduced to a truncated de-marked-up dump.
  * Golden fixture (c) — an all-telemetry message falls back to
    rule_based_summary (never silence).
  * FLAG-OFF PARITY — with speech_selection.enabled=false, route() takes the
    byte-identical qwen summarize() path; the selector is never invoked.
  * FLAG-ON WIRING — with enabled=true, route() uses the selector and never
    calls the LLM.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from daemon.content_router import ContentRouter
from daemon.tts_types import Category
from daemon.ollama_summarizer import rule_based_summary

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
# Optional drift check against a live training capture. Env-driven: unset (the
# normal case, and every CI run) means the bundled fixture is the only source of
# truth. An absolute path here would be both personal and unusable elsewhere.
_LIVE_CAPTURE = (
    Path(os.environ["CLAUDE_TTS_TRAINING_CAPTURE"])
    if os.environ.get("CLAUDE_TTS_TRAINING_CAPTURE")
    else None
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _SpyProvider:
    """Records whether summarize()/judge() were called and returns a marker so a
    test can prove which code path produced the spoken text."""

    inner_timeout_s = 2.0

    def __init__(self, marker: str = "QWEN_SUMMARY") -> None:
        self.marker = marker
        self.summarize_calls = 0
        self.judge_calls = 0

    async def summarize(self, content, category, context_hint=""):
        self.summarize_calls += 1
        return self.marker

    async def judge(self, *a, **k):
        self.judge_calls += 1
        return False


def _selector_router(enabled: bool, provider=None) -> ContentRouter:
    return ContentRouter({"speech_selection": {"enabled": enabled}}, provider=provider)


def _load_6a466f1f() -> dict:
    """Byte-exact motivating record. Prefer the bundled fixture; if the live
    capture is present, assert the bundled input_text still matches it (drift
    guard) — the selector is exercised on the same text the daemon saw."""
    bundled = json.loads(
        (_FIXTURE_DIR / "speech_selection_6a466f1f.json").read_text(encoding="utf-8")
    )
    if _LIVE_CAPTURE and _LIVE_CAPTURE.exists():
        for line in _LIVE_CAPTURE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("event_id", "").startswith("6a466f1f"):
                assert d["input_text"] == bundled["input_text"], (
                    "bundled 6a466f1f fixture drifted from the live capture record"
                )
                break
    return bundled


# --------------------------------------------------------------------------- #
# HARD GATE — the motivating record
# --------------------------------------------------------------------------- #
def test_hard_gate_6a466f1f_blockquote_substance_survives():
    rec = _load_6a466f1f()
    router = _selector_router(enabled=True)

    # Precondition: the input carries the blockquote substance, and the ORIGINAL
    # spoken_text at capture DROPPED it — that is the bug this selector replaces.
    assert "constant back and forth" in rec["input_text"]
    assert "constant back and forth" not in rec.get("spoken_text_at_capture", "")

    spoken = router._select_spoken_text(rec["input_text"])

    # The exact substance the qwen path dropped now survives selection.
    assert "constant back and forth" in spoken
    # And it is not merely a truncated de-marked-up dump: no markdown artifacts.
    assert "**" not in spoken
    assert ">" not in spoken


# --------------------------------------------------------------------------- #
# Golden fixture (b) — plain multi-paragraph majority class
# --------------------------------------------------------------------------- #
_PLAIN_REPLY = (
    "The root cause was a stale **cache entry** that survived the reload because "
    "the key omitted the project scope.\n\n"
    "Including the scope in the key makes every later read consistent, so the "
    "flaky reload now settles on the first try.\n\n"
    "That also retires the manual cache-clear you ran each morning."
)


def test_plain_multiparagraph_not_reduced_to_truncated_dump():
    router = _selector_router(enabled=True)
    spoken = router._select_spoken_text(_PLAIN_REPLY)

    # Substance from EVERY paragraph is present — not head-truncated to the first
    # sentence like a naive 200-char rule_based_summary would be.
    assert "cache entry" in spoken          # paragraph 1
    assert "flaky reload" in spoken         # paragraph 2
    assert "each morning" in spoken         # paragraph 3 (the tail)
    # Clean speech: markdown stripped, ends at a real sentence boundary (no "…").
    assert "**" not in spoken
    assert spoken.rstrip()[-1] in ".!?"
    assert not spoken.rstrip().endswith("...")
    # It genuinely differs from the head-biased 200-char baseline.
    assert spoken != rule_based_summary(_PLAIN_REPLY)


# --------------------------------------------------------------------------- #
# Golden fixture (c) — all-telemetry falls back, never silence
# --------------------------------------------------------------------------- #
_ALL_TELEMETRY = (
    "711 passed / 5 skipped\n\n"
    "Build succeeded. 0 errors, 3 warnings.\n\n"
    "23 passed, 4 failed, 1 skipped."
)


def test_all_telemetry_falls_back_to_rule_based_summary():
    router = _selector_router(enabled=True)
    spoken = router._select_spoken_text(_ALL_TELEMETRY)

    # No substantive block -> defer to the shipped rule_based_summary baseline…
    assert spoken == rule_based_summary(_ALL_TELEMETRY)
    # …and never silence a turn that had content.
    assert spoken.strip()


def test_never_silences_content_bearing_turn():
    router = _selector_router(enabled=True)
    # A message whose only prose is fenced code (all non-speakable blocks) must
    # still fall back rather than return empty.
    msg = "```python\ndef f():\n    return 1\n```\n\n```\nmore code\n```"
    spoken = router._select_spoken_text(msg)
    assert spoken == rule_based_summary(msg)


# --------------------------------------------------------------------------- #
# FLAG-OFF PARITY — byte-identical qwen path, selector untouched
# --------------------------------------------------------------------------- #
def _long_stop_event() -> dict:
    return {
        "command": "stop_event",
        "content": "This is a substantial assistant turn summary that easily "
                   "clears the 120-char summarization threshold and would be "
                   "routed to the summarizer under the default configuration.",
        "session_id": "s-parity",
        "event_id": "evt-parity",
    }


def test_flag_off_uses_qwen_summarize_selector_untouched():
    provider = _SpyProvider(marker="QWEN_SUMMARY")
    router = _selector_router(enabled=False, provider=provider)

    # Spy on the selector so we can prove it is NOT invoked when the flag is off.
    calls = {"selector": 0}
    original = router._select_spoken_text

    def _tracked(content):
        calls["selector"] += 1
        return original(content)

    router._select_spoken_text = _tracked  # type: ignore[assignment]

    item = asyncio.run(router.route(_long_stop_event()))

    assert item is not None
    # Flag OFF: the qwen summarize() path ran, and its output is spoken verbatim —
    # byte-identical to today's behavior. The selector was never touched.
    assert provider.summarize_calls == 1
    assert calls["selector"] == 0
    assert item.decision.content == "QWEN_SUMMARY"


def test_flag_on_uses_selector_no_llm():
    provider = _SpyProvider(marker="QWEN_SUMMARY")
    router = _selector_router(enabled=True, provider=provider)

    calls = {"selector": 0}
    original = router._select_spoken_text

    def _tracked(content):
        calls["selector"] += 1
        return original(content)

    router._select_spoken_text = _tracked  # type: ignore[assignment]

    item = asyncio.run(router.route(_long_stop_event()))

    assert item is not None
    # Flag ON: the selector produced the spoken text; the LLM was never called.
    assert calls["selector"] == 1
    assert provider.summarize_calls == 0
    assert item.decision.content != "QWEN_SUMMARY"
    assert item.decision.content.strip()


def test_config_defaults_flag_off():
    """A router built from an empty config defaults the selector OFF (so the
    live daemon is unaffected until the owner opts in)."""
    router = ContentRouter({})
    assert router._speech_selection_enabled is False
    assert router._sel_max_chars == 400
    assert router._sel_max_list_items == 3
    assert router._sel_max_heading_level == 2
    assert router._sel_cta_search_limit == 240
