import json
import logging
import os
import stat
from unittest.mock import MagicMock, patch

import pytest

from daemon.tts_daemon import TTSDaemon


def _daemon():
    d = TTSDaemon.__new__(TTSDaemon)
    d.logger = logging.getLogger("tts_daemon_test_audit")
    d.log = lambda msg, level="INFO": None
    d._config_error = None
    d._tts_user_config = {}
    d._pipeline_adapter = d._content_router = d._queue_manager = None
    d._ollama_summarizer = d._ollama_client = None
    d.stats = {"requests_received": 0, "daemon_start": 0}
    return d


def test_broken_config_does_not_fall_through(tmp_path, monkeypatch):
    bad = tmp_path / "config.json"
    bad.write_text("{not json")
    monkeypatch.setenv("CLAUDE_TTS_CONFIG", str(bad))
    d = _daemon()
    assert d._load_tts_user_config() == {}
    assert "config.json" in d._config_error


def test_bad_voice_rate_leaves_pipeline_down_and_health_broken(monkeypatch):
    d = _daemon()
    d._load_tts_user_config = lambda: {"voice": {"rate": "fast"}}
    d._start_pipeline_singletons()
    assert d._pipeline_adapter is None
    d.resource_monitor = MagicMock()
    d.resource_monitor.get_memory_usage.return_value = 1
    d.resource_monitor.get_cpu_percent.return_value = 1
    d.session_queues = {}
    d.error_counts = {}
    d._get_last_audio_played_at = lambda: None
    d._tier_value = lambda s: 0
    for n in ("tts_circuit_breaker", "audio_circuit_breaker", "socket_circuit_breaker"):
        setattr(d, n, MagicMock(state=MagicMock(value="closed")))
    sent = []
    sock = MagicMock()
    sock.send.side_effect = sent.append
    with patch("daemon.tts_daemon._recv_line", return_value=b'{"command":"health"}\n'):
        d.handle_client(sock)
    reply = json.loads(sent[0])
    assert reply["pipeline_ready"] is False and reply["health"] == "broken"


def test_shadow_log_is_rotating_and_raw_excerpt_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(logging.getLogger("tts_daemon.shadow"), "handlers", [])
    d = _daemon()
    d._skip_excerpt = lambda kind, req: "SECRET OUTPUT"
    d._log_shadow_decision("tool_event", {"event_id": "e"}, None)
    monkeypatch.setenv("AGENT_TTS_SHADOW_RAW", "1")
    d._log_shadow_decision("tool_event", {"event_id": "e"}, None)
    h = logging.getLogger("tts_daemon.shadow").handlers[0]
    assert isinstance(h, logging.handlers.RotatingFileHandler)
    h.flush()
    lines = [json.loads(x) for x in (tmp_path / ".claude/logs/tts/shadow.log").read_text().splitlines()]
    assert "skipped_excerpt" not in lines[0] and lines[1]["skipped_excerpt"] == "SECRET OUTPUT"
    logging.getLogger("tts_daemon.shadow").handlers.clear()


def test_shadow_skipped_len_logged_without_raw_env(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(logging.getLogger("tts_daemon.shadow"), "handlers", [])
    monkeypatch.delenv("AGENT_TTS_SHADOW_RAW", raising=False)
    d = _daemon()
    d._skip_excerpt = lambda kind, req: "SECRET OUTPUT"
    d._log_shadow_decision("tool_event", {"event_id": "e"}, None)
    logging.getLogger("tts_daemon.shadow").handlers[0].flush()
    line = json.loads((tmp_path / ".claude/logs/tts/shadow.log").read_text().splitlines()[0])
    assert line["skipped_len"] == len("SECRET OUTPUT") and "skipped_excerpt" not in line
    logging.getLogger("tts_daemon.shadow").handlers.clear()


def test_socket_is_owner_only(monkeypatch):
    path = f"/tmp/audit-{os.getpid()}.sock"
    monkeypatch.setattr("daemon.tts_daemon.SOCKET_PATH", path)
    d = _daemon()
    d.socket_server_loop = lambda: None
    d.start_socket_server()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    d.server_socket.close()
    os.unlink(path)


def test_ollama_skipped_for_null_provider():
    d = _daemon()
    d._load_tts_user_config = lambda: {"llm_provider": {"type": "null"}}
    with patch("daemon.pipeline.adapter.PipelineAdapter") as pa:
        pa.return_value._orchestrator = None
        d._start_pipeline_singletons()
    assert d._ollama_client is None and d._ollama_summarizer is None
