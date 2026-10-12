"""Cross-session playback serialization must serialize WITHOUT stalling the loop.

WHY THIS FILE WAS REWRITTEN (2026-10-11)
The original five tests guarded a *mechanism*, not a property: a blocking fcntl
lock, offloaded with asyncio.to_thread so it would not block the event loop.
That mechanism was deliberately deleted (daemon/pipeline/playback_stage.py,
"LEGACY-08: _acquire_legacy_global_lock, _release_legacy_global_lock — all
deleted (Phase 3)"), replaced by an in-process asyncio.Lock. So all five tests
asserted the existence of methods that no longer exist, and all five had been
red since 2026-07-04 — unnoticed, because this file was not in `make verify`.
It is now.

The property they were protecting is still load-bearing, and it has two halves
that pull against each other:

  1. only ONE session's audio may play at a time (otherwise clips overlap), and
  2. waiting for that must not stall the event loop (otherwise every other
     session's hooks, timers and socket reads freeze while audio plays).

A blocking lock satisfies (1) and destroys (2); no lock satisfies (2) and
destroys (1). The tests below assert BOTH, against whatever primitive does the
job today, and deliberately name no helper that a future refactor can delete out
from under them. That is the mistake being corrected here: the old tests could
not survive their own subject being renamed.

Note this is weaker than the original in one honest respect — it no longer pins
a specific call shape at a specific callsite, so a refactor that keeps both
properties is free to proceed without touching this file. That is the intent.
"""
import ast
import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daemon.pipeline.playback_stage import PlaybackStage, PlaybackState  # noqa: E402

PLAYBACK = Path(__file__).resolve().parent.parent / "daemon" / "pipeline" / "playback_stage.py"


class TestSerializationPrimitive(unittest.IsolatedAsyncioTestCase):
    """The primitive must be awaitable, so waiting for it yields to the loop."""

    async def test_cross_session_primitive_is_awaitable(self):
        stage = PlaybackStage()
        lock = getattr(stage, "_cross_session_audio_lock", None)
        self.assertIsNotNone(
            lock,
            "PlaybackStage has no cross-session audio lock. Something must "
            "serialize playback across sessions or concurrent sessions will "
            "talk over each other.",
        )
        self.assertIsInstance(
            lock, asyncio.Lock,
            f"the cross-session audio lock is a {type(lock).__name__}, not an "
            "asyncio.Lock. A blocking primitive (threading.Lock, fcntl.flock) "
            "acquired on the event-loop thread freezes every other session for "
            "the duration of playback. If a blocking lock is genuinely needed, "
            "it must be awaited off-thread (asyncio.to_thread) — and then this "
            "test needs rewriting, not deleting.",
        )


class TestNoBlockingLockInPlaybackModule(unittest.IsolatedAsyncioTestCase):
    """Source guard: the playback module must not reach for a blocking lock."""

    async def test_playback_module_imports_no_blocking_lock(self):
        """Deny the IMPORT, not the call shape.

        Denying `threading.Lock` textually was the first version of this, and a
        mutation test walked straight through it: `import threading as _t` then
        `_t.Lock()` matched nothing. Denying the import catches every alias and
        every call shape at once, and costs nothing here because this module has
        no legitimate use for either library — it is asyncio throughout.

        Honest limit: this cannot see a blocking lock constructed elsewhere and
        handed to PlaybackStage. The isinstance check above is what catches that.
        """
        tree = ast.parse(PLAYBACK.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        for forbidden in ("fcntl", "threading"):
            self.assertNotIn(
                forbidden, imported,
                f"{PLAYBACK.name} imports {forbidden!r}, whose locks block the "
                "calling thread — and this module runs on the event loop, so a "
                "blocking acquire freezes every other session for the duration "
                "of playback. The cross-process fcntl tier was deleted on "
                "purpose (LEGACY-08): the daemon's singleton PID check already "
                "guarantees one daemon per user. If you genuinely need one "
                "back, it must be offloaded with asyncio.to_thread — and then "
                "this test needs rewriting, not deleting.",
            )


class TestSerializesAndDoesNotStall(unittest.IsolatedAsyncioTestCase):
    """Both halves of the property, measured in one run."""

    async def test_playback_serializes_without_stalling_the_event_loop(self):
        """Both halves in one run — with one limitation worth knowing.

        If the lock is replaced by a genuinely BLOCKING one, this test HANGS
        rather than fails: the first task blocks the loop thread, so the
        `wait_for` timeout below can never fire. Measured, not assumed. That is
        precisely why the two structural guards above exist and are not
        redundant with this one — they turn that hang into a fast, readable
        failure. This test's own binding is the other direction: delete the
        serialization and it fails immediately with max_concurrent == 3.
        """
        stage = PlaybackStage()

        ticks = 0
        concurrent = 0
        max_concurrent = 0

        async def heartbeat():
            nonlocal ticks
            for _ in range(25):
                await asyncio.sleep(0.02)
                ticks += 1

        async def fake_inner(audio_path, state=None, request_id=None):
            # Stands in for real playback: holds the cross-session lock for a
            # while. Real playback awaits a subprocess; this awaits a sleep.
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            try:
                await asyncio.sleep(0.15)
                return True
            finally:
                concurrent -= 1

        stage._play_audio_inner = fake_inner

        beat = asyncio.create_task(heartbeat())
        players = [
            asyncio.create_task(stage._play_audio("/dev/null", PlaybackState()))
            for _ in range(3)
        ]
        await asyncio.wait_for(asyncio.gather(beat, *players), timeout=10.0)

        # Half 1: serialization actually happened.
        self.assertEqual(
            max_concurrent, 1,
            f"{max_concurrent} sessions played at once. The cross-session lock "
            "is not serializing playback, so concurrent sessions will talk "
            "over each other.",
        )
        # Half 2: and the event loop stayed free while they waited.
        self.assertGreaterEqual(
            ticks, 20,
            f"event loop was stalled: only {ticks}/25 heartbeat ticks landed "
            "while three sessions contended for the playback lock. Waiting for "
            "audio must not freeze the loop — every other session's hooks, "
            "timers and socket reads run on it.",
        )


if __name__ == "__main__":
    unittest.main()
