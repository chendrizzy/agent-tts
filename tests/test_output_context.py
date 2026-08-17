"""Tests for output_context — dynamic project / artifact / intent weaving.

Deterministic GATE (router decides WHEN) split from PHRASING (LLM on the
summarized path, terse lead-in on the pass-through path). The whole feature is
gated behind ``output_context.enabled`` (default OFF); the single most important
invariant is that with the flag OFF the pipeline output is byte-identical.

Pure/sync where possible — no live Ollama, so this runs inside `make verify`.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from daemon.content_router import ContentRouter  # noqa: E402
from daemon.providers.ollama_provider import OllamaProvider  # noqa: E402
from daemon.tts_types import Category, RouterDecision  # noqa: E402
from daemon import spoken_log  # noqa: E402
import daemon.ollama_summarizer as summ_mod  # noqa: E402


class _MockSummarizer:
    """Records summarize() args; returns a fixed string. Wrapped by
    OllamaProvider and passed as provider=."""

    def __init__(self, response: str = "[mock summary]") -> None:
        self.response = response
        self.calls: list[tuple[str, Category, str]] = []

    async def summarize(self, content, category, context_hint="", allow_fallback=True):
        self.calls.append((content, category, context_hint))
        return self.response


def _router(enabled: bool) -> ContentRouter:
    return ContentRouter(
        config={"output_context": {"enabled": enabled}},
        provider=OllamaProvider(_MockSummarizer()),
    )


def _decision(content, *, category=Category.STATUS, needs_sum=False, hint=""):
    return RouterDecision(
        should_speak=True,
        category=category,
        content=content,
        priority=5,
        source_event_id="",
        classified_at=0.0,
        needs_summarization=needs_sum,
        context_hint=hint,
        raw_excerpt=content[:80],
    )


# --------------------------------------------------------------------------- #
# Test 1 — _project_label basename + generic-root skip                         #
# --------------------------------------------------------------------------- #

def test_project_label_basename_and_generic_roots():
    assert ContentRouter._project_label("/Users/x/auth-service") == "auth-service"
    assert ContentRouter._project_label("/Users/x/auth-service/") == "auth-service"
    assert ContentRouter._project_label("relative/path/svc") == "svc"
    for generic in ("", "  ", "/", ".", "tmp", "/tmp", None, 42):
        assert ContentRouter._project_label(generic) == "", generic


# --------------------------------------------------------------------------- #
# Test 2 — C2 project gate                                                     #
# --------------------------------------------------------------------------- #

def _ev(cwd, session_id="s", command="tool_event"):
    return {"command": command, "cwd": cwd, "session_id": session_id, "tool_input": {}}


def test_c2_same_project_labels_only_first():
    r = _router(True)
    ev = _ev("/repo/alpha")
    d1 = r._apply_output_context(_decision("bash output here"), ev)
    assert d1.content == "In alpha, bash output here"
    # Same project again → no second label (consecutive same-project never repeats).
    d2 = r._apply_output_context(_decision("more output here"), ev)
    assert d2.content == "more output here"


def test_c2_error_always_labels_even_same_project():
    r = _router(True)
    ev = _ev("/repo/alpha")
    r._apply_output_context(_decision("first"), ev)  # primes per-project recency
    d = r._apply_output_context(_decision("disk is full", category=Category.ERROR), ev)
    assert d.content == "In alpha, disk is full"


def test_c2_switch_relabels():
    r = _router(True)
    r._apply_output_context(_decision("the build finished"), _ev("/repo/alpha"))
    # beta is a FIRST mention → named (a switch to a NOT-yet-named project).
    d = r._apply_output_context(_decision("tests are green"), _ev("/repo/beta"))
    assert d.content == "In beta, tests are green"


def test_c2_content_already_names_project_no_label():
    r = _router(True)
    d = r._apply_output_context(_decision("the alpha build finished"), _ev("/repo/alpha"))
    assert d.content == "the alpha build finished"
    # State still refreshed so a later same-project item is correctly suppressed.
    assert r._last_spoken_project == "alpha"


# --------------------------------------------------------------------------- #
# Test 2b — re-anchor lenience (name again after a quiet gap, no switch)       #
# --------------------------------------------------------------------------- #

class _Clock:
    """Deterministic monotonic clock for the re-anchor tests."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _oc_router(reanchor_gap_s, clock):
    r = ContentRouter(
        config={"output_context": {"enabled": True, "reanchor_gap_s": reanchor_gap_s}},
        provider=OllamaProvider(_MockSummarizer()),
    )
    r._clock = clock
    return r


def test_reanchor_relabels_same_project_after_gap():
    clk = _Clock(1000.0)
    r = _oc_router(90, clk)
    ev = _ev("/repo/alpha")
    # First naming at t=1000.
    assert r._apply_output_context(_decision("bash output here"), ev).content == (
        "In alpha, bash output here"
    )
    # +30s: within the 90s gap → still suppressed.
    clk.t = 1030.0
    assert r._apply_output_context(_decision("more output here"), ev).content == (
        "more output here"
    )
    # +130s since last naming (> 90s) → re-anchored even though no switch.
    clk.t = 1130.0
    assert r._apply_output_context(_decision("even more output"), ev).content == (
        "In alpha, even more output"
    )


def test_reanchor_disabled_when_zero():
    clk = _Clock(0.0)
    r = _oc_router(0, clk)
    ev = _ev("/repo/alpha")
    r._apply_output_context(_decision("first output here"), ev)  # named once
    clk.t = 100_000.0  # arbitrarily long gap
    # gap disabled → same project stays suppressed regardless of elapsed time.
    assert r._apply_output_context(_decision("second output here"), ev).content == (
        "second output here"
    )


# --------------------------------------------------------------------------- #
# Test 3 — artifact path unchanged (_maybe_prefix_target still fires / skips)  #
# --------------------------------------------------------------------------- #

def test_artifact_prefix_unchanged():
    # Fires: short, generic content + a target → "In <target>: ...".
    assert (
        ContentRouter._maybe_prefix_target("23 passed, 4 failed.", "test_router")
        == "In test_router: 23 passed, 4 failed."
    )
    # Skips: content already self-explanatory (>=60 chars).
    long = "x" * 60
    assert ContentRouter._maybe_prefix_target(long, "test_router") == long
    # Skips: no target hint.
    assert ContentRouter._maybe_prefix_target("done.", "") == "done."


# --------------------------------------------------------------------------- #
# Test 4 — _intent_marker                                                      #
# --------------------------------------------------------------------------- #

def test_intent_proceeding():
    assert ContentRouter._intent_marker(
        "The regression is isolated; I'll patch it now.", Category.FINAL_ANSWER
    ) == "proceeding"
    assert ContentRouter._intent_marker(
        "Applying the migration.", Category.FINAL_ANSWER
    ) == "proceeding"


def test_intent_needs_input():
    assert ContentRouter._intent_marker(
        "Should I proceed with the migration?", Category.FINAL_ANSWER
    ) == "needs_input"
    assert ContentRouter._intent_marker(
        "Let me know which branch to target.", Category.INSIGHT
    ) == "needs_input"


def test_intent_neither():
    assert ContentRouter._intent_marker("The tests all passed.", Category.FINAL_ANSWER) is None


def test_intent_trailing_question_mark():
    assert ContentRouter._intent_marker(
        "Which file did you mean", Category.FINAL_ANSWER
    ) is None
    assert ContentRouter._intent_marker(
        "Which file did you mean?", Category.FINAL_ANSWER
    ) == "needs_input"


def test_intent_prose_categories_only():
    # STATUS / ERROR are not prose — no intent marker regardless of phrasing.
    assert ContentRouter._intent_marker("I'll fix it now.", Category.STATUS) is None
    assert ContentRouter._intent_marker("Should I retry?", Category.ERROR) is None


def test_needs_input_beats_proceeding():
    # "let me know" (needs_input) must win over a "let me ... fix" reading.
    assert ContentRouter._intent_marker(
        "Let me know if I should fix this.", Category.FINAL_ANSWER
    ) == "needs_input"


# --------------------------------------------------------------------------- #
# Test 5 — master gate OFF => output byte-identical (the safety test)          #
# --------------------------------------------------------------------------- #

_CORPUS = [
    {  # short FINAL_ANSWER prose that WOULD get lead-in + intent when ON
        "command": "stop_event",
        "session_id": "oc-off-1",
        "content": "The login regression is isolated; I'll patch it now.",
        "event_id": "e1",
        "ts": 1.0,
    },
    {  # STATUS tool result (short, pass-through) with a cwd
        "command": "tool_event",
        "phase": "post",
        "event_id": "e2",
        "ts": 2.0,
        "session_id": "oc-off-2",
        "cwd": "/repo/alpha",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tests/test_router.py"},
        "tool_response": {"stdout": "23 passed, 4 failed\n", "stderr": "", "interrupted": False},
    },
]


def test_flag_off_is_byte_identical():
    spoken_log.note_session_cwd("oc-off-1", "/repo/alpha")  # would resolve a project when ON
    r_off = _router(False)
    for ev in _CORPUS:
        item = asyncio.run(r_off.route(dict(ev)))
        assert item is not None, ev["event_id"]
        c = item.decision.content
        # No lead-in, no intent cue, no structured facts leaked into the hint.
        assert " — " not in c
        assert "Fixing now." not in c and "Need your input." not in c
        assert "project=" not in item.decision.context_hint
        assert "intent=" not in item.decision.context_hint
    # The gate never armed → context-switch state stays pristine.
    assert r_off._last_spoken_project == ""


def test_flag_on_actually_changes_output():
    # Proves the OFF test above is meaningful: the SAME event diverges when ON.
    spoken_log.note_session_cwd("oc-on-1", "/repo/alpha")
    ev = {
        "command": "stop_event",
        "session_id": "oc-on-1",
        "content": "The login regression is isolated; I'll patch it now.",
        "event_id": "e-on",
        "ts": 1.0,
    }
    off = asyncio.run(_router(False).route(dict(ev)))
    on = asyncio.run(_router(True).route(dict(ev)))
    assert off.decision.content == "The login regression is isolated; I'll patch it now."
    # Short/pass-through: natural project lead-in only. The intent ("I'll patch
    # it now") is already in the prose, so NO redundant "Fixing now." is appended.
    assert on.decision.content == (
        "In alpha, The login regression is isolated; I'll patch it now."
    )
    assert on.decision.content != off.decision.content


# --------------------------------------------------------------------------- #
# Summarized path — structured facts land on context_hint (not the content)   #
# --------------------------------------------------------------------------- #

def test_summarized_path_appends_facts_to_hint():
    r = _router(True)
    ev = _ev("/repo/alpha")
    d = r._apply_output_context(
        _decision("a" * 200, category=Category.FINAL_ANSWER, needs_sum=True, hint="final answer"),
        {**ev, "command": "stop_event"},
    )
    assert d.context_hint.startswith("final answer")
    assert "project=alpha" in d.context_hint
    assert d.content == "a" * 200  # content untouched; the LLM does the weaving


def test_woven_summary_not_flagged_as_prompt_echo():
    # A legitimate summary that weaves project/file must not trip the echo guard,
    # and the new tokens are not echo signatures.
    assert summ_mod._looks_like_prompt_echo("In auth-service, I fixed login.ts.") is False
    assert summ_mod._looks_like_prompt_echo("project=x; file=y; intent=proceeding") is False


# --------------------------------------------------------------------------- #
# Intent cue is reserved for the SUMMARIZED path (non-redundancy refinement)   #
# --------------------------------------------------------------------------- #

def test_pass_through_omits_redundant_intent_cue():
    # Short/pass-through: intent was detected FROM the content, so the prose
    # already conveys it — no "Fixing now."/"Need your input." is appended.
    r = _router(True)
    ev = {"command": "stop_event", "session_id": "no-cwd-intent-pt"}  # no project
    d1 = r._apply_output_context(
        _decision("I'll patch the handler now.", category=Category.FINAL_ANSWER),
        ev,
    )
    assert d1.content == "I'll patch the handler now."  # unchanged
    d2 = r._apply_output_context(
        _decision("Should I retry the migration?", category=Category.FINAL_ANSWER),
        ev,
    )
    assert d2.content == "Should I retry the migration?"  # unchanged


def test_summarized_preserves_intent_in_hint():
    # Summarized: the LLM rewrite might drop the intent, so it's preserved as a
    # directive on context_hint for the prompt cue.
    r = _router(True)
    d = r._apply_output_context(
        _decision(
            "I'll patch the handler now." + " more detail" * 40,
            category=Category.FINAL_ANSWER,
            needs_sum=True,
            hint="final answer",
        ),
        {"command": "stop_event", "session_id": "no-cwd-intent-sum"},
    )
    assert "intent=proceeding" in d.context_hint


# --------------------------------------------------------------------------- #
# Per-project voice identity (project_voices)                                  #
# --------------------------------------------------------------------------- #

def _voice_router(pool, enabled=True):
    return ContentRouter(
        config={
            "output_context": {"enabled": True},
            "project_voices": {"enabled": enabled, "pool": pool},
        },
        provider=OllamaProvider(_MockSummarizer()),
    )


def test_project_voice_is_deterministic_and_stable():
    pool = ["v1", "v2", "v3"]
    r1, r2 = _voice_router(pool), _voice_router(pool)
    # Same project -> same voice, across router instances (restart-stable).
    assert r1._resolve_project_voice("alpha") == r2._resolve_project_voice("alpha")
    assert r1._resolve_project_voice("alpha") in pool
    # A spread of projects uses more than one voice from the pool.
    voices = {r1._resolve_project_voice(p) for p in ("alpha", "beta", "gamma", "delta")}
    assert voices <= set(pool) and len(voices) >= 2
    # Disabled or unresolvable -> None (daemon falls back to the default voice).
    assert _voice_router(pool, enabled=False)._resolve_project_voice("alpha") is None
    assert r1._resolve_project_voice("") is None


def test_project_voice_malformed_pool_returns_none():
    r = ContentRouter(
        config={"project_voices": {"enabled": True, "pool": "not-a-list"}},
        provider=OllamaProvider(_MockSummarizer()),
    )
    assert r._resolve_project_voice("alpha") is None


def test_project_voice_applies_when_output_context_off():
    pool = ["v1", "v2", "v3"]
    r = ContentRouter(
        config={
            "output_context": {"enabled": False},
            "project_voices": {"enabled": True, "pool": pool},
        },
        provider=OllamaProvider(_MockSummarizer()),
    )
    ev = {
        "command": "stop_event",
        "session_id": "s1",
        "event_id": "e1",
        "content": "done now",
        "cwd": "/repo/alpha",
    }
    item = asyncio.run(r.route(ev))
    assert item is not None
    assert item.decision.content == "done now"
    assert item.decision.voice == r._resolve_project_voice("alpha")


def test_voice_is_stashed_on_decision():
    r = _voice_router(["v1", "v2", "v3"])
    d = r._apply_project_voice(_decision("bash out here"), _ev("/repo/alpha"))
    assert d.voice == r._resolve_project_voice("alpha")


def test_voice_off_leaves_decision_voice_none():
    # Non-voice mode (the base output_context tests' regime) never sets a voice.
    r = _router(True)
    d = r._apply_output_context(_decision("bash out here"), _ev("/repo/alpha"))
    assert d.voice is None


def test_voice_mode_prepend_is_first_mention_only():
    # With voice identity on, the verbal prepend names a project only the FIRST
    # time; later switches back rely on the voice, so no repeated "alpha —".
    # (Content is chosen NOT to contain the project name, so the redundancy guard
    # isn't what's suppressing the prepend — the first-mention gate is.)
    r = _voice_router(["v1", "v2", "v3"])
    a1 = r._apply_output_context(_decision("the build finished"), _ev("/repo/alpha"))
    assert a1.content == "In alpha, the build finished"    # introduce alpha
    b1 = r._apply_output_context(_decision("tests are green"), _ev("/repo/beta"))
    assert b1.content == "In beta, tests are green"         # introduce beta
    a2 = r._apply_output_context(_decision("deploy queued"), _ev("/repo/alpha"))
    assert a2.content == "deploy queued"                   # switch back: voice carries it
    # ERROR still names the project verbally, for clarity.
    ae = r._apply_output_context(
        _decision("disk is full", category=Category.ERROR), _ev("/repo/alpha")
    )
    assert ae.content == "In alpha, disk is full"


# --------------------------------------------------------------------------- #
# Project ROOT resolution — a project is its repo, not a per-folder basename    #
# --------------------------------------------------------------------------- #

def test_project_root_collapses_subdirs_to_the_repo(tmp_path):
    # A repo root (marked by .git) and ALL its subdirs resolve to one label.
    root = tmp_path / "myproject"
    (root / ".git").mkdir(parents=True)
    sub = root / "services" / "auth"
    sub.mkdir(parents=True)
    r = _voice_router(["v1", "v2", "v3"])
    assert r._project_label(r._project_root(str(root))) == "myproject"
    assert r._project_label(r._project_root(str(sub))) == "myproject"


def test_voice_same_for_all_subdirs_of_one_repo(tmp_path):
    # The rule: subdirectories of ONE repo get the SAME voice; only separate
    # repos differ. web/ and api/ under one root must NOT get different voices.
    root = tmp_path / "myproject"
    (root / ".git").mkdir(parents=True)
    (root / "web").mkdir()
    (root / "api").mkdir()
    r = _voice_router(["v1", "v2", "v3"])
    d_web = r._apply_project_voice(_decision("built ok"), _ev(str(root / "web")))
    d_api = r._apply_project_voice(_decision("tests pass"), _ev(str(root / "api")))
    assert d_web.voice == d_api.voice and d_web.voice is not None  # one repo, one voice
    # A separate repo resolves to its OWN root (its own voice slot).
    other = tmp_path / "otherproj"
    (other / ".git").mkdir(parents=True)
    d_other = r._apply_project_voice(_decision("done"), _ev(str(other)))
    assert d_other.voice == r._resolve_project_voice("otherproj")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
