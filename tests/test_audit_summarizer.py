"""Audit regressions: Ollama read timeout, provider fallback/caps, factory guards."""
import asyncio
import collections
import logging
import socket
import time

from daemon.ollama_integration import OllamaClient
from daemon.ollama_summarizer import _MAX_CONTENT_CHARS, OllamaSummarizer
from daemon.providers.factory import make_provider
from daemon.providers.null_provider import NullProvider
from daemon.providers.openai_compat import OpenAICompatProvider
from daemon.tts_types import Category


def test_generate_response_timeout_frees_the_thread():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)  # accepts at the kernel level, never replies
    c = OllamaClient.__new__(OllamaClient)
    c.metrics = collections.defaultdict(int)
    c.logger = logging.getLogger("t")
    c.api_base = f"http://127.0.0.1:{srv.getsockname()[1]}"
    t = time.monotonic()
    try:
        assert c.generate_response("hi", model="m", timeout=0.5) is None
    finally:
        srv.close()
    assert 0.4 < time.monotonic() - t < 5  # waited the read timeout, not 180 s


def test_openai_failure_falls_back_and_caps_content():
    p = OpenAICompatProvider("http://x", "m")
    seen = []

    async def chat(prompt):
        seen.append(prompt)
        return None

    p._chat = chat
    big = "Tests passed. " * 2000
    out = asyncio.run(p.summarize(big, Category.STATUS))
    assert out and len(out) < len(big)
    assert len(seen[0]) < _MAX_CONTENT_CHARS + 500
    assert asyncio.run(p.summarize(big, Category.STATUS, allow_fallback=False)) is None


def test_ollama_prompt_is_capped():
    prompts = []

    class C:
        def generate_response(self, prompt, *a, **k):
            prompts.append(prompt)
            return "ok."

    asyncio.run(OllamaSummarizer(C()).summarize("x" * 30000, Category.STATUS))
    assert len(prompts[0]) < 30000 - 20000


def test_factory_bad_config_degrades_to_null():
    assert isinstance(make_provider({"llm_provider": "ollama"}, None), NullProvider)
    assert isinstance(make_provider({"llm_provider": {"type": "openai", "timeout_s": "fast"}}, None), NullProvider)
    assert isinstance(make_provider({"llm_provider": {"type": "bogus"}}, object()), NullProvider)


def test_long_log_keeps_result_tail_all_paths():
    log = "noise " * 3500 + "FAILED 3, 120 passed"
    seen = []

    class C:
        def generate_response(self, prompt, *a, **k):
            seen.append(prompt)
            return "ok."

    s = OllamaSummarizer(C())
    asyncio.run(s.summarize(log, Category.STATUS))
    assert "FAILED 3, 120 passed" in seen[0]

    from daemon.tts_types import RoutedItem
    from types import SimpleNamespace as NS
    items = [RoutedItem.__new__(RoutedItem) for _ in range(2)]
    for it in items:
        object.__setattr__(it, "decision", NS(content="x" * 2000 + "FAILED 3", category=Category.STATUS))
    asyncio.run(s.condense_batch(items))
    assert seen[1].count("FAILED 3") == 2

    p = OpenAICompatProvider("http://x", "m")
    got = []

    async def chat(prompt):
        got.append(prompt)
        return "ok"

    p._chat = chat
    asyncio.run(p.summarize(log, Category.STATUS))
    assert "FAILED 3, 120 passed" in got[0]


def test_openai_empty_content_falls_back():
    p = OpenAICompatProvider("http://x", "m")

    async def chat(prompt):
        return "  "

    p._chat = chat
    assert asyncio.run(p.summarize("Tests passed. All good here.", Category.STATUS))
    assert asyncio.run(p.summarize("Tests passed.", Category.STATUS, allow_fallback=False)) is None


def test_warmup_passes_read_timeout():
    calls = []

    class C:
        def generate_response(self, *a, **k):
            calls.append(a)
            return "x"

    s = OllamaSummarizer(C(), timeout_s=2.0)
    assert asyncio.run(s.warmup())
    assert calls[0][-1] == 60.0
