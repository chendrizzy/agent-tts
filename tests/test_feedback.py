"""Feedback layer: signatures, decay, resets, adjust rules, CLI, router wiring.

Hermetic: AGENT_TTS_CONTROL_DIR and spoken_log.SPOKEN_DIR point at tmp_path.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daemon import feedback as fb  # noqa: E402
from daemon import session_control as sc  # noqa: E402
from daemon import spoken_log  # noqa: E402
from daemon.content_router import ContentRouter  # noqa: E402
from daemon.tts_types import Category, RouterDecision  # noqa: E402

CLI = ROOT / "daemon" / "session_control.py"
DAY = 86400.0
NOW = 1_800_000_000.0  # pinned so decay never perturbs threshold arithmetic


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TTS_CONTROL_DIR", str(tmp_path / "ctl"))
    monkeypatch.setattr(spoken_log, "SPOKEN_DIR", tmp_path / "spoken")
    fb._CACHE["sig"] = None
    sc._IDENT_CACHE.clear()


def _put(label, *, project="/p", category="status", text="Tests passed: 12 of 40", weight=1.0,
         ts=NOW, sig=None, source="panel"):
    rec = {"ts": ts, "sid": "s", "project": project,
           "category": category, "signature": sig or fb.signature(text), "label": label,
           "weight": weight, "source": source, "text": text}
    fb._append(rec)


def test_signature_examples():
    assert fb.signature("In my-proj, 12 tests passed in /a/b/c.py") == "# tests passed in <path>"
    assert fb.signature("Build 4f3a9c2e1b failed after 3.5 seconds") == "build <hex> failed after # seconds"
    assert fb.signature('Found "foo bar" in 7 files') == 'found "…" in # files'
    assert fb.signature("Run 123e4567-e89b-12d3-a456-426614174000 ok") == "run <hex> ok"
    assert fb.signature("one two three four five six seven eight nine ten") == \
        "one two three four five six seven eight"
    assert fb.signature("Tests passed: 12 of 40") == fb.signature("Tests passed: 3 of 9")


def test_decay_halves_every_14_days():
    now = 1_000_000_000.0
    _put("useful", ts=now - 14 * DAY)
    t = fb._tally(fb._load()["all"], now)
    assert t["useful"] == pytest.approx(0.5)


def test_reset_zeroes_earlier_only():
    _put("noise", ts=NOW - 2)
    _put("noise", ts=NOW - 1, text="Other thing happened here")
    fb.forget("/p", fb.signature("Tests passed: 1 of 2"))
    _put("useful", ts=NOW + 1)
    recs = fb._load()["all"]
    assert [r["label"] for r in recs] == ["noise", "useful"]
    fb.forget("/p", "*", "status")
    assert fb._load()["all"] == []


def test_suppress_threshold_and_error_exception():
    text = "Tests passed: 12 of 40"
    _put("noise")
    assert not fb.adjust("/p", "status", text, NOW)["suppress"]
    _put("noise")
    assert fb.adjust("/p", "status", text, NOW)["suppress"]
    assert not fb.adjust("/p", "error", text, NOW)["suppress"]  # -2 > -3
    _put("noise", category="error")
    assert fb.adjust("/p", "error", text, NOW)["suppress"]
    assert not fb.adjust("/q", "status", "Totally different words appear", NOW)["suppress"]


def test_suppress_needs_noise_to_outweigh():
    for _ in range(2):
        _put("noise")
    for _ in range(3):
        _put("useful")
    assert not fb.adjust("/p", "status", "Tests passed: 12 of 40", NOW)["suppress"]


def test_global_fallback_counts_half():
    _put("noise", project="/other")
    _put("noise", project="/other")
    _put("noise", project="/other")
    r = fb.adjust("/p", "status", "Tests passed: 12 of 40", NOW)  # -3 * 0.5 = -1.5
    assert r["suppress"]
    _put("useful", project="/p")  # own votes now exist -> no fallback
    assert not fb.adjust("/p", "status", "Tests passed: 12 of 40", NOW)["suppress"]


def test_urgent_boost_and_category_delta():
    _put("urgent")
    r = fb.adjust("/p", "status", "Tests passed: 12 of 40", NOW)
    assert r["priority_delta"] == 3 + 2  # template +3, category round(3*1.5/3)=2
    assert r["priority_delta"] <= fb.DELTA_MAX
    for _ in range(30):
        _put("noise", text="zzz unrelated message here")
    assert fb.adjust("/p", "status", "fresh unseen sentence", NOW)["priority_delta"] == -3


def test_verbose_summarize_factor():
    assert fb.adjust("/p", "status", "x", NOW)["summarize_factor"] == 1.0
    _put("verbose")
    assert fb.adjust("/p", "status", "Tests passed: 12 of 40", NOW)["summarize_factor"] == pytest.approx(0.7 ** 2)
    for _ in range(10):
        _put("verbose")
    assert fb.adjust("/p", "status", "y", NOW)["summarize_factor"] == 0.35


def test_fast_path_does_no_git_or_io(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(fb, "_load", boom)
    assert fb.adjust("/p", "status", "hello 1") == {
        "suppress": False, "reason": "", "priority_delta": 0, "summarize_factor": 1.0,
        "signature": "hello #"}


def test_adjust_never_raises(monkeypatch):
    _put("noise")
    monkeypatch.setattr(fb, "_load", lambda: 1 / 0)
    assert fb.adjust("/p", "status", "x", NOW)["suppress"] is False


def test_mtime_cache_reloads_on_change():
    _put("useful")
    assert len(fb._load()["all"]) == 1
    _put("useful")
    assert len(fb._load()["all"]) == 2


def test_implicit_pause_last_90s_only(tmp_path):
    now = time.time()
    for ts, text in ((now - 200, "old one"), (now - 60, "recent one"), (now - 5, "newest one")):
        spoken_log.append(text, session_id="sid1", category="status", ts=ts)
    assert fb.implicit_pause("sid1", str(tmp_path), now=now) == 2
    recs = fb._load()["all"]
    assert [(r["text"], r["label"], r["weight"], r["source"]) for r in recs] == [
        ("recent one", "noise", 0.3, "implicit"), ("newest one", "noise", 0.3, "implicit")]
    assert fb.implicit_pause("nope", str(tmp_path), now=now) == 0


def test_record_validates_and_truncates(tmp_path):
    with pytest.raises(ValueError):
        fb.record("bogus", "s", "", "status", "t")
    rec = fb.record("useful", "s", "", "status", "a" * 500)
    assert rec["project"] == "" and len(rec["text"]) == 300


def _cli(*args):
    p = subprocess.run([sys.executable, str(CLI), *args], text=True, capture_output=True, env=os.environ)
    return p.returncode, json.loads(p.stdout)


def test_cli_round_trip(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    for text in ("Tests passed: 12 of 40", "Tests passed: 3 of 9"):
        rc, out = _cli("feedback", "--label", "noise", "--sid", "s", "--cwd", str(proj),
                       "--category", "status", "--text", text)
        assert rc == 0 and out["recorded"]["label"] == "noise"
    assert out["effect"]["suppress"] is True
    rc, t = _cli("tuning")
    assert t["templates"][0]["state"] == "suppressed"
    assert t["templates"][0]["project"] == out["recorded"]["project"]
    rc, f = _cli("forget", "--project", out["recorded"]["project"],
                 "--signature", out["recorded"]["signature"])
    assert (rc, f) == (0, {"ok": True})
    assert _cli("tuning")[1]["templates"] == []
    rc, bad = _cli("feedback", "--label", "nope", "--sid", "s", "--cwd", "", "--category", "c", "--text", "t")
    assert rc == 2 and "error" in bad


def test_cli_implicit_pause_and_mute_does_not_record(tmp_path):
    spoken_log.append("just now", session_id="sx", category="insight")
    rc, out = _cli("mute", "sx")
    assert rc == 0 and not (tmp_path / "ctl" / "feedback.jsonl").exists()
    env_dir = str(tmp_path / "spoken")
    p = subprocess.run([sys.executable, "-c",
                        "from daemon import spoken_log as s, feedback as f; from pathlib import Path;"
                        f"s.SPOKEN_DIR=Path({env_dir!r}); print(f.implicit_pause('sx',''))"],
                       text=True, capture_output=True, cwd=ROOT, env=os.environ)
    assert p.stdout.strip() == "1"


def _routed(router, event, content, needs_sum=False):
    dec = RouterDecision(should_speak=True, category=Category.STATUS, content=content, priority=5,
                         source_event_id="e", classified_at=0.0, needs_summarization=needs_sum)

    async def classify(_event):
        return dec
    router.classify_event = classify
    return asyncio.run(router.route(event))


def test_router_drops_rated_noise_template_only(tmp_path):
    router = ContentRouter(config={})
    event = {"session_id": "s", "cwd": str(tmp_path)}
    project = sc.identify(str(tmp_path))["project"]
    assert _routed(router, event, "Tests passed: 5 of 9") is not None  # no feedback file: neutral
    for text in ("Tests passed: 12 of 40", "Tests passed: 3 of 9"):
        fb.record("noise", "s", str(tmp_path), "status", text)
    assert _routed(router, event, "Tests passed: 7 of 8") is None
    assert router._last_drop_reason == "feedback: template 'tests passed # of #' rated noise"
    item = _routed(router, event, "Deploy finished successfully")
    assert item is not None and item.decision.priority in (3, 4, 5)  # category drifted down, never to zero
    assert project  # identity resolved


def test_router_priority_and_summarize(tmp_path):
    router = ContentRouter(config={})
    event = {"session_id": "s", "cwd": str(tmp_path)}
    fb.record("urgent", "s", str(tmp_path), "status", "Deploy finished successfully")
    assert _routed(router, event, "Deploy finished successfully").decision.priority >= 8
    for _ in range(3):
        fb.record("verbose", "s", str(tmp_path), "insight", "x")
    long = "word " * (router.summarize_threshold // 5)
    item = _routed(router, event, "Deploy finished successfully")
    assert item.decision.needs_summarization is False
    dec = RouterDecision(should_speak=True, category=Category.INSIGHT, content=long[:int(router.summarize_threshold * 0.6)],
                         priority=5, source_event_id="e", classified_at=0.0)
    out = asyncio.run(router._apply_feedback(dec, event))
    assert out.needs_summarization is True


# --- review-fix regressions --------------------------------------------------
def test_signature_strips_output_context_prefix():
    assert fb.signature("In my-proj, 12 tests passed") == fb.signature("12 tests passed") == "# tests passed"
    assert fb.signature("In agent-tts: 12 tests passed") == "# tests passed"


def test_signature_unicode_and_empty():
    assert fb.signature("!!! ...") == "" and fb.signature("🎉🎉") == ""
    assert fb.signature("テストが通りました") == "テストが通りました"
    assert fb.signature("Тесты прошли: 5") == "тесты прошли #"


def test_empty_signature_skips_template_verdict():
    for _ in range(4):
        _put("noise", text="🎉", sig="")
    assert fb.adjust("/p", "status", "🎉", now=NOW)["suppress"] is False
    for _ in range(3):
        _put("urgent", category="status", text="🎉🎉", sig="")
    assert fb.adjust("/p", "status", "🎉🎉", now=NOW)["priority_delta"] <= 3  # category only, no template boost


def test_rating_prefixed_form_suppresses_raw_router_form_and_vice_versa(tmp_path):
    router = ContentRouter(config={})
    event = {"session_id": "s", "cwd": str(tmp_path)}
    for _ in range(2):
        fb.record("noise", "s", str(tmp_path), "status", "In my-proj, 12 tests passed")
    assert _routed(router, event, "7 tests passed") is None
    fb._path().unlink()
    fb._CACHE["sig"] = None
    for _ in range(2):
        fb.record("noise", "s", str(tmp_path), "status", "12 tests passed")
    dec = RouterDecision(should_speak=True, category=Category.STATUS, content="In agent-tts: 3 tests passed",
                         priority=5, source_event_id="e", classified_at=0.0)
    assert asyncio.run(router._feedback_suppresses(dec, event)) is True


def test_route_rechecks_final_spoken_content(tmp_path):
    router = ContentRouter(config={})
    event = {"session_id": "s", "cwd": str(tmp_path)}
    for _ in range(2):
        fb.record("noise", "s", str(tmp_path), "status", "Condensed result")
    long = "word " * (router.summarize_threshold // 5 + 5)

    async def summarize(d):
        return replace(d, content="Condensed result", needs_summarization=False)
    router._maybe_summarize = summarize
    router._speech_selection_enabled = False
    assert _routed(router, event, long, needs_sum=True) is None
    assert router._last_drop_reason.startswith("feedback:")


def test_implicit_pause_votes_once_per_utterance_and_never_suppresses(tmp_path):
    now = time.time()
    for i in range(5):
        spoken_log.append(f"line {i} done", session_id="sid1", category="status", ts=now - 10 - i)
    counts = [fb.implicit_pause("sid1", str(tmp_path), now=now) for _ in range(5)]
    assert counts == [5, 0, 0, 0, 0]
    assert len(fb._load()["all"]) == 5
    spoken_log.append("line 9 done", session_id="sid1", category="status", ts=now - 1)
    assert fb.implicit_pause("sid1", str(tmp_path), now=now) == 1
    # even with many implicit-only votes (templates grouped by signature), nothing is suppressed
    for _ in range(20):
        _put("noise", text="Same template here", source="implicit")
    assert fb.adjust("/p", "status", "Same template here", now=NOW)["suppress"] is False


def test_forget_compacts_and_decayed_records_dropped():
    _put("noise", text="Old thing", ts=time.time() - 400 * DAY)
    _put("useful", text="Keep this one", ts=time.time())
    _put("noise", text="Reset me please", ts=time.time())
    fb.forget("/p", fb.signature("Reset me please"))
    lines = fb._path().read_text().splitlines()
    assert [json.loads(x)["text"] for x in lines] == ["Keep this one"]


def test_record_compacts_past_size_limit(monkeypatch):
    monkeypatch.setattr(fb, "COMPACT_BYTES", 2000)
    old = {"ts": time.time() - 400 * DAY, "sid": "s", "project": "/p", "category": "status",
           "signature": "gone soon", "label": "noise", "weight": 1.0, "source": "panel", "text": "Gone soon"}
    fb._path().parent.mkdir(parents=True, exist_ok=True)
    fb._path().write_text("".join(json.dumps(old) + "\n" for _ in range(30)))
    n_before = len(fb._path().read_text().splitlines())
    fb.record("useful", "s", "", "status", "fresh")
    lines = fb._path().read_text().splitlines()
    assert n_before == 30 and len(lines) == 1 and json.loads(lines[0])["text"] == "fresh"


def test_route_identify_runs_off_event_loop(tmp_path, monkeypatch):
    import threading
    fb.record("useful", "s", str(tmp_path), "status", "Something")
    seen = []
    real = sc.identify
    monkeypatch.setattr(sc, "identify", lambda cwd: (seen.append(threading.current_thread()), real(cwd))[1])
    sc._IDENT_CACHE.clear()
    router = ContentRouter(config={})
    assert _routed(router, {"session_id": "s", "cwd": str(tmp_path)}, "Something else") is not None
    assert seen and all(t is not threading.main_thread() for t in seen)


def _old_reduce(lines):  # the original O(records x votes) semantics, as the oracle
    recs: list = []
    for line in lines:
        try:
            r = json.loads(line)
            if r["label"] not in fb.LABELS:
                continue
            float(r["ts"]), float(r["weight"])
        except (ValueError, KeyError, TypeError):
            continue
        vote = (r.get("line_id"), r.get("axis"))
        if r.get("line_id") and r.get("axis"):
            recs = [x for x in recs if (x.get("line_id"), x.get("axis")) != vote]
        if r["label"] == "clear":
            continue
        if r["label"] == "reset":
            if r.get("signature") == "*":
                recs = [x for x in recs if not (x["project"] == r["project"] and (
                    not r.get("category") or x["category"] == r["category"]))]
            else:
                recs = [x for x in recs if not (x["project"] == r["project"] and x["signature"] == r["signature"])]
        else:
            recs.append(r)
    return recs


def _rec(label, i, line_id=None, axis=None, project="/p", category="status", sig="s"):
    r = {"ts": NOW + i, "project": project, "category": category, "signature": sig,
         "label": label, "weight": 1.0}
    if line_id:
        r.update(line_id=line_id, axis=axis)
    return json.dumps(r)


def test_reduce_matches_original_semantics_on_mixed_sequence():
    lines = [
        _rec("noise", 0), _rec("useful", 1, "L1", "a"), _rec("noise", 2, "L1", "b"),
        _rec("useful", 3, "L1", "a"),            # re-vote replaces and moves to the end
        _rec("clear", 4, "L1", "b"),             # clear deletes the vote
        _rec("noise", 5, project="/q"), _rec("useful", 6, "L2", "a", project="/q"),
        _rec("reset", 7, project="/q", sig="*"),  # wipes /q legacy and votes
        _rec("useful", 8, "L3", "a", sig="t"), _rec("reset", 9, sig="t"),
        _rec("noise", 10, "L4", "a"), "not json", '{"label": "bogus", "ts": 1, "weight": 1}',
        _rec("useful", 11, "L4", "a"), _rec("noise", 12),
    ]
    assert fb._reduce(lines) == _old_reduce(lines)


def test_reduce_scales_linearly_with_vote_records():
    lines = [_rec("noise", i, f"L{i}", "a") for i in range(20000)]
    t0 = time.perf_counter()
    assert len(fb._reduce(lines)) == 20000
    assert time.perf_counter() - t0 < 1.0


def test_reduce_tolerates_a_non_string_vote_key():
    # A hand-edited line with a list line_id must not take down every tuning lookup.
    bad = json.dumps({"ts": 1.0, "label": "noise", "weight": 1.0, "line_id": ["a"], "axis": "relevance",
                      "project": "p", "category": "status", "signature": "s"})
    assert len(fb._reduce([bad])) == 1
