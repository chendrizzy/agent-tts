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

from daemon.paths import config_candidates, config_path

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


def _atomic_write(dest: Path, cfg: dict) -> None:
    """Write cfg to dest via a 0600 tmp file in the same dir + os.replace (never a torn file)."""
    dest = dest.resolve()  # a symlinked config stays a symlink; its target is what changes
    tmp = dest.with_name(f"{dest.name}.tmp.{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except PermissionError:  # read-only dir, writable file: not atomic, but better than failing
        with open(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cfg, indent=2) + "\n")
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cfg, indent=2) + "\n")
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_config(cfg: dict, path: Optional[Path] = None) -> Path:
    """Serialize cfg to `path` (default config_path()) with a 0600 file; a dir it creates is 0700."""
    from daemon.session_control import locked  # lazy: session_control imports this module
    dest = Path(path) if path is not None else config_path()
    with locked("config.lock"):  # same lock as update_config: no lost update
        made = not dest.parent.exists()
        dest.parent.mkdir(parents=True, exist_ok=True)
        if made:  # never chmod a pre-existing folder (it may be the user's own, e.g. ~)
            os.chmod(dest.parent, 0o700)
        _atomic_write(dest, cfg)
    return dest


_SOURCE_TREE = Path(__file__).resolve().parent.parent


def effective_config_path() -> Path:
    """The file the daemon reads (first existing candidate), else config_path() for a new one."""
    return next((p for p in config_candidates() if p.exists()), config_path())


def active_config_path() -> Path:
    """Where writes go: the effective file, unless it is the in-repo default -> config_path()."""
    eff = effective_config_path()
    return config_path() if eff.resolve().is_relative_to(_SOURCE_TREE) else eff


# key -> validator returning the stored value; ValueError text goes straight to the panel.
def _number(key: str, lo: float, hi: float):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"{key}: expected a number {lo}-{hi}, got {v!r}")
        return float(v)
    return check


def _enabled(v):
    if not isinstance(v, bool):
        raise ValueError(f"project_voices.enabled: expected true or false, got {v!r}")
    return v


def _pool(v):
    if (not isinstance(v, list) or not 1 <= len(v) <= 64
            or not all(isinstance(x, str) and x.strip() for x in v)):
        raise ValueError("project_voices.pool: expected 1-64 non-empty strings")
    return v


def update_config(key: str, value) -> dict:
    """Set one editable key in the config the daemon reads; return the new config.

    Raises ValueError (file untouched) for an unknown key, a bad value, or a file
    that does not parse -- a broken file is never overwritten.
    """
    from daemon.session_control import locked, voices_info  # lazy: session_control imports this module
    if key == "voice.name":
        group, field = "voice", "name"
    elif key in ("voice.rate", "voice.volume", "project_voices.enabled", "project_voices.pool"):
        group, field = key.split(".")
    else:
        raise ValueError(f"unknown key: {key!r}")
    check = {"voice.rate": _number(key, 0.5, 2.0), "voice.volume": _number(key, 0.0, 2.0),
             "project_voices.enabled": _enabled, "project_voices.pool": _pool}.get(key)
    with locked("config.lock"):
        src, path = effective_config_path(), active_config_path()  # read what the daemon reads
        cfg: dict = {}
        if src.exists():
            try:
                cfg = json.loads(src.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise ValueError(f"{src}: {e}") from e
            if not isinstance(cfg, dict):
                raise ValueError(f"{src}: not a JSON object")
        if check:
            value = check(value)
        elif not isinstance(value, str) or not value.strip():
            raise ValueError("voice.name: expected a non-empty voice name")
        section = cfg.get(group)
        if section is not None and not isinstance(section, dict):
            raise ValueError(f"{group}: expected an object in the config, got {section!r}")
        if key in ("voice.name", "project_voices.pool"):
            info = voices_info(cfg)
            engine = info["engine"] or "edge-tts"  # same default as config_state
            if engine in VOICE_CATALOGS:
                known = set(info["catalog"]) | {n for g in VOICE_CATALOGS[engine].values() for n in g}
                bad = [v for v in ([value] if key == "voice.name" else value) if v not in known]
                if bad:
                    raise ValueError(f"{key}: unknown voice {bad[0]!r} for this engine")
        cfg[group] = section = section if section is not None else {}
        section[field] = value
        made = not path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if made:
            os.chmod(path.parent, 0o700)
        _atomic_write(path, cfg)
    return cfg
