"""Assemble + persist the portable claude-tts user config.

render_config is pure (caller-supplied values only — no personal data, no
absolute paths). write_config serializes to config_path() (or an explicit path)
with owner-only perms, since the config may hold an API key. The schema matches
what daemon/tts_daemon._load_tts_user_config reads (provider selector is
`llm_provider.type`; voice is `voice.engine` + `voice.name`).
"""
from __future__ import annotations

import json
import os
from itertools import cycle, islice
from pathlib import Path
from typing import Literal, Optional

from daemon.paths import config_path

GenderPreference = Literal[
    "balanced",
    "female_only",
    "male_only",
    "female_preferred",
    "male_preferred",
]
ProjectVoiceScope = Literal["shared", "per_project"]

SUPPORTED_POLICY_ENGINES = {"edge-tts", "kokoro"}

VOICE_CATALOGS: dict[str, dict[str, list[str]]] = {
    "edge-tts": {
        "female": [
            "en-US-AvaNeural",
            "en-US-EmmaMultilingualNeural",
            "en-US-JennyNeural",
            "en-US-AriaNeural",
        ],
        "male": [
            "en-US-GuyNeural",
            "en-US-BrianMultilingualNeural",
            "en-US-ChristopherNeural",
            "en-US-AndrewMultilingualNeural",
        ],
    },
    "kokoro": {
        "female": ["af_heart", "af_bella", "bf_emma"],
        "male": ["am_michael", "bm_george"],
    },
}


def _normalize_gender_preference(value: str) -> GenderPreference:
    valid = {
        "balanced",
        "female_only",
        "male_only",
        "female_preferred",
        "male_preferred",
    }
    normalized = value.lower().strip()
    if normalized not in valid:
        raise ValueError(f"unsupported voice gender preference: {value}")
    return normalized  # type: ignore[return-value]


def _normalize_project_voice_scope(value: str) -> ProjectVoiceScope:
    normalized = value.lower().strip().replace("-", "_")
    aliases = {
        "same": "shared",
        "all_projects": "shared",
        "single": "shared",
        "varying": "per_project",
        "per_project": "per_project",
        "shared": "shared",
    }
    try:
        return aliases[normalized]  # type: ignore[return-value]
    except KeyError as exc:
        raise ValueError(f"unsupported project voice scope: {value}") from exc


def _interleave(left: list[str], right: list[str]) -> list[str]:
    slots: list[str] = []
    longest = max(len(left), len(right))
    for index in range(longest):
        if index < len(left):
            slots.append(left[index])
        if index < len(right):
            slots.append(right[index])
    return slots


def _weighted_preferred_pool(preferred: list[str], alternate: list[str]) -> list[str]:
    preferred_slots = list(islice(cycle(preferred), len(alternate) * 4))
    return _interleave(preferred_slots, alternate)


def _policy_pool(engine: str, preference: GenderPreference,
                 fallback_voice: str) -> tuple[list[str], float, bool]:
    catalog = VOICE_CATALOGS.get(engine)
    if not catalog:
        return [fallback_voice], 0.0, False

    female = catalog["female"]
    male = catalog["male"]
    if preference == "balanced":
        slot_count = max(len(female), len(male))
        female_slots = list(islice(cycle(female), slot_count))
        male_slots = list(islice(cycle(male), slot_count))
        return _interleave(female_slots, male_slots), 0.5, True
    if preference == "female_only":
        return list(female), 1.0, True
    if preference == "male_only":
        return list(male), 1.0, True
    if preference == "female_preferred":
        return _weighted_preferred_pool(female, male), 0.8, True
    return _weighted_preferred_pool(male, female), 0.8, True


def _gender_compatible_voice(engine: str, voice_name: str,
                             preference: GenderPreference) -> str:
    catalog = VOICE_CATALOGS.get(engine)
    if not catalog or preference == "balanced":
        return voice_name

    target_gender = "female" if preference.startswith("female") else "male"
    target_voices = catalog[target_gender]
    if voice_name in target_voices:
        return voice_name
    return target_voices[0]


def render_config(*, engine: str, voice_name: str, backend: str,
                  model: str = "", base_url: str = "", api_key: str = "",
                  project_voice_scope: str = "shared",
                  voice_gender_preference: str = "balanced") -> dict:
    """Build the config dict for the chosen engine + LLM backend."""
    scope = _normalize_project_voice_scope(project_voice_scope)
    gender_preference = _normalize_gender_preference(voice_gender_preference)
    supports_policy = engine in SUPPORTED_POLICY_ENGINES
    voice_name = _gender_compatible_voice(engine, voice_name, gender_preference)
    pool, preferred_ratio, gender_enforced = _policy_pool(
        engine, gender_preference, voice_name,
    )

    cfg: dict = {
        "voice": {"engine": engine, "name": voice_name, "rate": 1.0, "volume": 1.0},
        "project_voices": {
            "enabled": scope == "per_project" and supports_policy,
            "gender_preference": gender_preference,
            "preferred_ratio": preferred_ratio,
            "gender_enforced": gender_enforced,
            "pool": pool,
        },
    }
    if not supports_policy:
        cfg["project_voices"]["_note"] = (
            "Per-project and gender assignment are enforceable only for "
            "edge-tts and kokoro. This engine uses the shared voice.name."
        )

    backend = backend.lower()
    if backend == "null":
        cfg["llm_provider"] = {"type": "null"}
    elif backend == "openai":
        cfg["llm_provider"] = {
            "type": "openai", "base_url": base_url, "model": model, "api_key": api_key,
        }
    else:  # ollama
        cfg["llm_provider"] = {"type": "ollama"}
        cfg["summarizer"] = {"model": model, "inner_timeout_s": 3.5,
                             "keep_alive": "30m", "warm_interval_s": 120.0}
    return cfg


def write_config(cfg: dict, path: Optional[Path] = None) -> Path:
    """Serialize cfg to `path` (default config_path()) with 0700 dir / 0600 file."""
    dest = Path(path) if path is not None else config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    dest.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.chmod(dest, 0o600)
    return dest
