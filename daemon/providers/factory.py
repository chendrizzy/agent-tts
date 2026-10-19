"""Select an LLMProvider from config. Default: local Ollama."""
from __future__ import annotations

import logging

from daemon.providers.base import LLMProvider
from daemon.providers.null_provider import NullProvider
from daemon.providers.ollama_provider import OllamaProvider
from daemon.providers.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)


def make_provider(config: dict, summarizer) -> LLMProvider:
    cfg = (config or {}).get("llm_provider", {}) if isinstance(config, dict) else {}
    if not isinstance(cfg, dict):
        logger.warning("llm_provider config is not a mapping (%r); using NullProvider", cfg)
        return NullProvider()
    kind = str(cfg.get("type", "ollama")).lower()

    if kind == "null":
        return NullProvider()
    if kind == "openai":
        try:
            timeout_s = float(cfg.get("timeout_s", 8.0))
        except (TypeError, ValueError):
            logger.warning("llm_provider.timeout_s=%r is not a number; using NullProvider", cfg.get("timeout_s"))
            return NullProvider()
        return OpenAICompatProvider(
            base_url=str(cfg.get("base_url", "")),
            model=str(cfg.get("model", "")),
            api_key=str(cfg.get("api_key", "")),
            timeout_s=timeout_s,
        )
    if kind != "ollama":
        logger.warning("unknown llm_provider.type %r; using NullProvider", kind)
        return NullProvider()
    # a missing summarizer means Ollama init failed.
    if summarizer is None:
        return NullProvider()
    return OllamaProvider(summarizer)
