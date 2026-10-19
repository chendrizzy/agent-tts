"""Global mood (session_control.MOODS) and per-line rating axes (feedback.vote/votes).

Hermetic via tests/conftest.py (AGENT_TTS_CONTROL_DIR -> tmp); spoken_log is not touched.
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from daemon import feedback as fb  # noqa: E402
from daemon import session_control as sc  # noqa: E402
from daemon.content_router import ContentRouter  # noqa: E402
from daemon.tts_types import Category, RouterDecision  # noqa: E402

CLI = ROOT / "daemon" / "session_control.py"
NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _reset_cache():
    fb._CACHE["sig"] = None


def _cli(*args, stdin=""):
    p = subprocess.run([sys.executable, str(CLI), *args], text=True, capture_output=True,
                       env=os.environ, input=stdin)
    return p.returncode, json.loads(p.stdout)


def _decide(category, content, router, priority=5):
    dec = RouterDecision(should_speak=True, category=category, content=content, priority=priority,
                         source_event_id="e", classified_at=0.0,
                         needs_summarization=len(content) >= router.summarize_threshold)

    async def classify(_event):
        return dec
    async def summarize(d):  # records the verdict that normally reaches the LLM; the flag is cleared after
        router.summarized = True
        return d
    router.classify_event, router._maybe_summarize, router.summarized = classify, summarize, False
    return asyncio.run(router.route({"session_id": "s"}))


def _tool(router, stdout, stderr=""):
    return asyncio.run(router.classify_event({
        "command": "tool_event", "tool_name": "Bash", "session_id": "s", "tool_input": {"command": "./x.sh"},
        "tool_response": {"stdout": stdout, "stderr": stderr}}))


LONG = "word " * 30  # 150 chars: over the 120 threshold
MID = "word " * 18   # 90 chars: under 120, over 60 and 120*0.5
AMBIG = "Deploy step 3 started with 12 workers\n" + "\n".join(f"line {c} of the thing" for c in "abcdef")


# --- mood CLI / storage -------------------------------------------------------
def test_mood_cli_default_set_invalid_and_snapshot():
    assert _cli("mood") == (0, {"mood": "normal"})
    assert _cli("mood", "--set", "chatty") == (0, {"mood": "chatty"})
    assert _cli("mood") == (0, {"mood": "chatty"})
    rc, bad = _cli("mood", "--set", "loud")
    assert rc == 2 and "unknown mood" in bad["error"]
    assert _cli("mood")[1] == {"mood": "chatty"}
    assert _cli("snapshot", stdin="{}")[1]["mood"] == "chatty"


def test_unknown_mood_file_reads_as_normal():
    sc.set_mood("quiet")
    (sc.paths.control_dir() / "mood").write_text("garbage")
    assert sc.mood() == "normal"


# --- mood mapping ---------------------------------------------------------------
@pytest.mark.parametrize("mood", sc.MOODS)
def test_errors_always_spoken(mood):
    sc.set_mood(mood)
    router = ContentRouter(config={})
    item = _decide(Category.ERROR, LONG, router, priority=10)
    assert item is not None and item.decision.priority == 10


def test_normal_changes_nothing():
    router = ContentRouter(config={})
    for cat in Category:
        for text in ("short text here", MID, LONG):
            base = _decide(cat, text, router)
            sc.set_mood("normal")
            assert _decide(cat, text, router).decision == base.decision
            if cat != Category.ERROR:
                assert router.summarized == (len(text) >= 120)
            assert base is not None


def test_quiet_drops_insight_and_status_keeps_final_answer_condensed():
    sc.set_mood("quiet")
    router = ContentRouter(config={})
    assert _decide(Category.STATUS, "Tests passed", router) is None
    assert router._last_drop_reason == "mood: status not spoken"
    assert _decide(Category.INSIGHT, "Found it", router) is None
    assert _decide(Category.FINAL_ANSWER, MID, router) is not None
    assert router.summarized is True  # normal would speak 90 chars verbatim


def test_chatty_and_narrate_condense_less():
    router = ContentRouter(config={})
    sc.set_mood("normal")
    for mood, text, expect in (("normal", LONG, True), ("chatty", LONG, False), ("chatty", "word " * 40, True),
                               ("narrate", "word " * 40, False)):  # thresholds 120 / 180 / 240
        sc.set_mood(mood)
        _decide(Category.FINAL_ANSWER, text, router)
        assert router.summarized is expect, (mood, len(text))


def test_quiet_shrinks_speech_selection_budget():
    router = ContentRouter(config={"speech_selection": {"enabled": True, "max_speech_duration_chars": 400}})
    text = "\n\n".join(f"Paragraph {i} says something rather useful and fairly long today." for i in range(12))
    sc.set_mood("normal")
    normal = _decide(Category.FINAL_ANSWER, text, router).decision.content
    sc.set_mood("quiet")
    quiet = _decide(Category.FINAL_ANSWER, text, router).decision.content
    assert 0 < len(quiet) < len(normal)


def test_tool_depth_per_mood():
    router = ContentRouter(config={})
    for mood, ambiguous, short in (("quiet", False, False), ("normal", False, False),
                                   ("chatty", True, False), ("narrate", True, True)):
        sc.set_mood(mood)
        assert _tool(router, AMBIG + mood).should_speak is ambiguous, mood
        assert _tool(router, "hello there friend " + mood).should_speak is short, mood


def test_narrate_still_drops_empty_and_duplicates():
    sc.set_mood("narrate")
    router = ContentRouter(config={})
    assert _tool(router, "").should_speak is False
    assert _tool(router, "hello there friend").should_speak is True
    assert _tool(router, "hello there friend").should_speak is False


def test_mood_is_read_live_per_event():
    router = ContentRouter(config={})
    sc.set_mood("quiet")
    assert _decide(Category.STATUS, "Tests passed", router) is None
    sc.set_mood("normal")
    assert _decide(Category.STATUS, "Tests passed", router) is not None


# --- rating axes ------------------------------------------------------------------
def _vote(line, axis, value, text="Tests passed: 12 of 40", cat="status"):
    return fb.vote(line, axis, value, "s", "", cat, text)


def test_vote_replaces_and_clear_removes():
    _vote("s:1.5", "relevance", "noise")
    _vote("s:1.5", "relevance", "relevant")
    assert [r["label"] for r in fb._load()["all"]] == ["useful"]
    assert fb.votes(["s:1.5"]) == {"s:1.5": {"relevance": "relevant"}}
    _vote("s:1.5", "relevance", "clear")
    assert fb._load()["all"] == [] and fb.votes(["s:1.5"]) == {}
    assert fb.adjust("", "status", "Tests passed: 1 of 2", now=NOW)["priority_delta"] == 0


def test_axes_are_independent_and_votes_omit_unvoted():
    _vote("s:1", "relevance", "noise")
    _vote("s:1", "length", "long")
    _vote("s:1", "priority", "lower")
    _vote("s:2", "priority", "higher")
    assert fb.votes(["s:1", "s:2", "s:3"]) == {
        "s:1": {"relevance": "noise", "length": "long", "priority": "lower"},
        "s:2": {"priority": "higher"}}
    assert sorted(r["label"] for r in fb._load()["all"]) == ["demote", "noise", "urgent", "verbose"]


def test_vote_validation():
    for args in (("", "relevance", "noise"), ("s:1", "bogus", "noise"), ("s:1", "length", "noise")):
        with pytest.raises(ValueError):
            _vote(*args)


def test_legacy_records_without_line_id_still_stack():
    fb.record("noise", "s", "", "status", "Tests passed: 12 of 40")
    fb.record("noise", "s", "", "status", "Tests passed: 3 of 9")
    assert len(fb._load()["all"]) == 2
    assert fb.adjust("", "status", "Tests passed: 5 of 9")["suppress"] is True
    fb.record("clear", "s", "", "status", "t")  # a bare clear carries no vote key: ignored, never crashes scoring
    assert len(fb._load()["all"]) == 2


def test_lower_priority_never_suppresses_alone():
    for i in range(6):
        _vote(f"s:{i}", "priority", "lower", text="Tests passed: 12 of 40")
    out = fb.adjust("", "status", "Tests passed: 5 of 9")
    assert out["suppress"] is False and out["priority_delta"] < 0


def test_short_only_undoes_condensing_never_above_one():
    _vote("s:1", "length", "short")
    assert fb.adjust("", "status", "Tests passed: 5 of 9")["summarize_factor"] == 1.0
    for i in range(3):
        _vote(f"s:v{i}", "length", "long")
    condensed = fb.adjust("", "status", "Tests passed: 5 of 9")["summarize_factor"]
    assert condensed < 1.0
    for i in range(3):
        _vote(f"s:c{i}", "length", "short")
    assert fb.adjust("", "status", "Tests passed: 5 of 9")["summarize_factor"] == 1.0


def test_tuning_shows_new_labels():
    _vote("s:1", "priority", "lower")
    _vote("s:2", "length", "short")
    t = fb.tuning()["templates"][0]
    assert t["demote"] > 0 and t["concise"] > 0


def test_votes_and_line_feedback_cli(tmp_path):
    args = ("--sid", "s", "--cwd", str(tmp_path), "--category", "status", "--text", "Tests passed: 12 of 40")
    rc, out = _cli("feedback", "--line", "s:9.25", "--axis", "length", "--value", "long", *args)
    assert rc == 0 and out["recorded"]["label"] == "verbose" and out["recorded"]["line_id"] == "s:9.25"
    assert out["effect"]["summarize_factor"] < 1.0
    assert _cli("feedback", "--line", "s:9.25", "--axis", "length", "--value", "short", *args)[1]["effect"][
        "summarize_factor"] == 1.0
    assert _cli("votes", "--lines", "s:9.25,s:none") == (0, {"votes": {"s:9.25": {"length": "short"}}})
    assert _cli("feedback", "--line", "s:9.25", "--axis", "length", "--value", "clear", *args)[0] == 0
    assert _cli("votes", "--lines", "s:9.25") == (0, {"votes": {}})
    assert _cli("feedback", "--line", "s:9.25", "--axis", "nope", "--value", "x", *args)[0] == 2
    assert _cli("feedback", "--line", "s:9.25", "--axis", "length", *args)[0] == 2


@pytest.mark.parametrize("text", [
    "The root cause was a stale cache; I fixed it and all tests pass. Want me to also deploy?",
    "Turns out the config was wrong, which means the retry never ran. All fixed.",
])
def test_quiet_speaks_stop_event_even_with_insight_phrasing(text):
    sc.set_mood("quiet")
    router = ContentRouter(config={})
    ev = {"command": "stop_event", "session_id": "s", "content": text}
    item = asyncio.run(router.route(ev))
    assert item is not None and item.decision.category == Category.INSIGHT  # spoken despite the insight label


def test_quiet_still_drops_tool_event_insight():
    sc.set_mood("quiet")
    router = ContentRouter(config={})
    assert _decide(Category.INSIGHT, "Found it", router) is None  # tool-style event (no stop_event command)
