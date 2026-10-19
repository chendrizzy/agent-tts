"""Router audit fixes #2 #8 #9 #10."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from daemon.content_router import ContentRouter, GREP_LINE_RE
from daemon.tts_types import Category, RouterDecision


def _router():
    return ContentRouter(config={})


def _bash(stdout, sid="s1"):
    return {"command": "tool_event", "session_id": sid, "tool_name": "Bash",
            "tool_input": {"command": "pytest"}, "tool_response": {"stdout": stdout},
            "event_id": "e"}


def _spoken(r, ev):
    d = asyncio.run(r.classify_event(ev))
    return d.should_speak, d.content, d.raw_excerpt


def test_clock_times_are_not_grep_lines():
    assert not GREP_LINE_RE.search("12:30:01 starting")
    assert GREP_LINE_RE.search("42:def foo():")


def test_test_results_survive_noisy_surroundings():
    for out in ("12:30:01 starting\n12:30:05 running suite\n23 passed in 4s",
                "On branch main\n  modified: 3 files\n23 passed in 4s"):
        ok, content, why = _spoken(_router(), _bash(out))
        assert ok, why
        assert "23 passed" in content


def test_dedupe_is_per_session():
    r = _router()
    r._note_hash("5 passed", "A")
    assert r._drop_check("5 passed", session_id="A") == "duplicate of recent content"
    assert r._drop_check("5 passed", session_id="B") is None


def test_dedupe_expires(monkeypatch):
    import daemon.content_router as cr
    r = _router()
    t = [1000.0]
    monkeypatch.setattr(cr.time, "monotonic", lambda: t[0])
    r._note_hash("5 passed", "A")
    t[0] += cr.ERROR_DEDUPE_WINDOW_S + 1
    assert r._drop_check("5 passed", session_id="A") is None


def test_project_voice_runs_off_the_loop(monkeypatch):
    from daemon import session_control
    seen = []

    async def go():
        loop_thread = __import__("threading").get_ident()
        monkeypatch.setattr(session_control, "override_voice",
                            lambda *a: seen.append(__import__("threading").get_ident()) or None)
        dec = RouterDecision(should_speak=True, category=Category.STATUS, content="x",
                             priority=5, source_event_id="e", classified_at=0.0)
        await _router()._apply_project_voice(dec, {"session_id": "s", "cwd": "/tmp"})
        return loop_thread

    assert seen == [] and asyncio.run(go()) != seen[0]


def test_cancelled_route_does_not_poison_retry():
    r = _router()
    ev = {"command": "stop_event", "session_id": "s", "content": "Done. Bug fixed.",
          "event_id": "e"}

    async def slow(_d):
        await asyncio.sleep(10)

    r._apply_feedback = lambda d, e: slow(d)  # stands in for a stage slower than the 5 s cap

    async def go():
        try:
            await asyncio.wait_for(r.route(ev), timeout=0.05)
        except asyncio.TimeoutError:
            pass

    asyncio.run(go())
    del r._apply_feedback
    assert asyncio.run(r.route(ev)) is not None  # retry still speaks
    # ...and counts once: the cancelled attempt left no tail run or window entry behind
    assert r._tail_runs["s"][1] == 1
    assert len(r._recent_spoken_by_session["s"]) == 1


_STOP = {"command": "stop_event", "session_id": "s", "content": "Done. Bug fixed.",
         "event_id": "e"}


def test_concurrent_duplicates_yield_one_item():
    r = _router()

    async def go():
        return await asyncio.gather(r.route(dict(_STOP)), r.route(dict(_STOP)))

    assert sum(i is not None for i in asyncio.run(go())) == 1


def test_backpressure_drop_releases_reservation():
    r = _router()
    r._pressure_for = lambda e: 5.0  # BLACK: drops everything but ERROR
    assert asyncio.run(r.route(dict(_STOP))) is None
    del r._pressure_for
    assert asyncio.run(r.route(dict(_STOP))) is not None
