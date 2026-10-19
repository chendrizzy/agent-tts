"""Audit regressions: idle session teardown, no-summarizer QueueManager, YELLOW pass-through."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from daemon.pipeline.ingest_stage import IngestMessage
from daemon.pipeline.orchestrator import TTSPipelineOrchestrator
from daemon.pipeline.queue_manager import QueueManager
from daemon.tts_types import Tier
from tests.test_queue_manager import _FakeIngestStage, _FakePlaybackStage, _FakePlaybackState


def _msg(rid="m1", content="hello", priority=7):
    return IngestMessage(content=content, session_id="s1", priority=priority,
                         request_id=rid, ingested_at=time.time())


def _qm(pending, summarizer=None):
    ingest, playback = _FakeIngestStage(), _FakePlaybackStage()
    playback.session_states["s1"] = _FakePlaybackState()
    for i in range(pending):
        ingest.add("s1", _msg(f"p{i}"))
    return QueueManager({}, summarizer, ingest, playback)


async def test_idle_session_tears_down_and_restarts():
    orch = TTSPipelineOrchestrator(cache_dir="/tmp/tts_test_cache", queue_manager=MagicMock())
    orch._session_idle_s = 0.05
    orch._running = True
    orch.ingest.consume = AsyncMock(return_value=None)
    orch.ingest.get_pending_count = MagicMock(return_value=0)
    for stage, name in ((orch.playback, "start_session"), (orch.playback, "stop_session"),
                        (orch.generate, "cleanup_session"), (orch.ingest, "cleanup_session")):
        setattr(stage, name, AsyncMock())
    orch.playback.session_buffers = {}
    orch._session_tasks["s1"] = task = asyncio.create_task(orch._process_session("s1"))
    orch.metrics.active_sessions = 1

    await asyncio.wait_for(task, 2)

    assert "s1" not in orch._session_tasks
    assert orch.metrics.active_sessions == 0
    orch.playback.stop_session.assert_awaited_once_with("s1")
    orch.generate.cleanup_session.assert_awaited_once_with("s1")
    orch.queue_manager.end_session.assert_called_once_with("s1")


def test_queue_manager_end_session_drops_state():
    qm = _qm(0)
    asyncio.run(qm.intercept(_msg(), "s1"))
    qm.end_session("s1")
    assert "s1" not in qm._sessions


def test_red_without_summarizer_uses_rule_based_merge():
    qm = _qm(3)  # 3 pending x 3500ms -> RED
    out = asyncio.run(qm.intercept(_msg(content="build done"), "s1"))
    assert qm.get_tier("s1") == Tier.RED
    assert out[0].content == "build done"


def test_yellow_passes_message_through_unmerged():
    qm = _qm(1)  # 3500ms -> YELLOW
    out = asyncio.run(qm.intercept(_msg(content="solo"), "s1"))
    assert qm.get_tier("s1") == Tier.YELLOW
    assert [m.content for m in out] == ["solo"]


def _mock_orch(**kw):
    orch = TTSPipelineOrchestrator(cache_dir="/tmp/tts_test_cache", queue_manager=MagicMock(), **kw)
    orch._running = True
    for stage, name in ((orch.playback, "start_session"), (orch.playback, "stop_session"),
                        (orch.generate, "cleanup_session"), (orch.ingest, "cleanup_session")):
        setattr(stage, name, AsyncMock())
    orch.playback.session_buffers = {}
    return orch


async def test_submit_during_teardown_waits_for_cleanup():
    orch = _mock_orch()
    orch._session_idle_s = 0.01

    async def consume(sid, timeout=None):
        await asyncio.sleep(0.005)  # a real consume yields; AsyncMock would spin the loop

    orch.ingest.consume = consume
    orch.ingest.get_pending_count = MagicMock(return_value=0)
    events, gate = [], asyncio.Event()

    async def slow_cleanup(sid):
        events.append("cleanup-start")
        await gate.wait()
        events.append("cleanup-end")

    async def ingest(message):
        events.append("ingest")
        return True

    orch.ingest.cleanup_session = slow_cleanup
    orch.ingest.ingest = ingest
    orch._session_tasks["s1"] = old = asyncio.create_task(orch._process_session("s1"))
    while "cleanup-start" not in events:
        await asyncio.sleep(0.01)

    orch._session_idle_s = 60  # the restarted session must outlive the assertions
    sub = asyncio.create_task(orch.submit("hi", session_id="s1"))
    await asyncio.sleep(0.05)
    assert "ingest" not in events  # would be wiped by the in-flight cleanup
    gate.set()
    await asyncio.wait_for(sub, 2)
    await old

    assert events == ["cleanup-start", "cleanup-end", "ingest"]
    assert "s1" in orch._session_tasks and orch._session_tasks["s1"] is not old
    assert not orch._teardowns
    orch._session_tasks["s1"].cancel()
    await asyncio.sleep(0)


async def test_lag_feed_is_per_message_total_audio():
    orch = _mock_orch()
    msg = _msg()
    orch.ingest.consume = AsyncMock(side_effect=[msg, asyncio.CancelledError()])
    orch.queue_manager.intercept = AsyncMock(return_value=[msg])
    orch.process.process = AsyncMock()

    async def gen(processed, voice):
        for _ in range(3):
            yield MagicMock(duration_ms=1000.0)

    orch.generate.generate = gen
    orch.playback.enqueue_segment = AsyncMock()
    await asyncio.wait_for(orch._process_session("s1"), 2)
    orch.queue_manager.update_avg_chunk_audio.assert_called_once_with(3000.0)
