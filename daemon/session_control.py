"""Per-session control layer: mute flags and scoped setting rules.

Stdlib-only leaf. Importable as ``daemon.session_control`` (daemon + hooks) and
runnable as ``python3 daemon/session_control.py <cmd>`` (the panel's CLI; every
command prints ONE JSON document on stdout, errors as ``{"error": msg}`` / exit 2).

State lives under ``paths.control_dir()``:
    muted/<safe_sid>   flag file, content ``{"since": <epoch>}``
    paused             global flag file, content ``{"since": <epoch>}``; the daemon drops lines while it exists
    mood               global file, content = one MOOD name; absent/unknown = normal. Read live per event.
    stay_down          flag file: Kill sets it, Boot/Restart clear it; hooks never auto-start while it exists
    voices.json        ``{"version": 2, "rules": {"<scope>:<key>": {<any of FIELDS>, "set_at"}}}``
                       (version 1 files, voice-only entries, read unchanged)

Mood (ONE global setting, never per scope) maps onto the router's existing knobs. Errors are
spoken in every mood. ``categories``: which Category values may speak. ``length``: scales the
summarize threshold (and the speech-selection char budget): below 1 condenses more, above 1
less. ``tools``: how much tool output is admitted: 0 = extractors + LLM judge (today), 1 = judge
bypassed (ambiguous digit+keyword output speaks), 2 = any non-empty output that survives the
noise/duplicate drop checks speaks.

    mood     categories                          length  tools
    quiet    error, final_answer                 0.5     0
    normal   error, final_answer, insight, status 1.0    0   (== today, byte for byte)
    chatty   error, final_answer, insight, status 1.5    1
    narrate  error, final_answer, insight, status 2.0    2

Rule scopes, narrowest first: session, branch, worktree, project, workspace,
voice (key = the session's resolved voice name), all. Each field resolves on its
own: the narrowest rule that sets THAT field wins, so a branch volume and a
project voice both apply. A voice-scope rule may not set the voice itself.
The daemon calls override_voice() on every event, so it must be cheap and must
never raise: no git subprocess unless a git-derived-scope rule sets the field.
"""
import sys

if __name__ == "__main__":  # script mode: repo root (not daemon/) must lead sys.path
    import os as _os
    sys.path[0] = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

import fcntl  # noqa: E402
import functools  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from daemon import config_io, paths  # noqa: E402

SCOPES = ("session", "branch", "worktree", "project", "workspace", "voice", "all")
# field -> (type, min, max); voice is a catalog name, the rest clamp into range.
FIELDS = {"voice": (str, None, None), "volume": (float, 0.0, 2.0), "rate": (float, 0.5, 2.0),
          "frequency": (int, 1, 5), "verbosity": (int, 1, 5)}
_GIT_SCOPES = ("branch", "worktree", "project", "workspace")
IDENTIFY_TTL_S = 30.0
_IDENTIFY_MAX = 512

_clock = time.monotonic  # injectable for tests
_IDENT_CACHE: dict = {}  # cwd -> (clock_ts, identity)
_RULES_CACHE: dict = {"sig": None, "rules": {}}


# --- ids / flags -------------------------------------------------------------
def safe_sid(sid) -> str:
    """Filesystem-safe session id: bytes outside [A-Za-z0-9_-] -> '_', max 128.

    Byte-wise on purpose so the hooks' ``tr -c 'A-Za-z0-9_-' '_' | cut -c1-128``
    (LC_ALL=C) produces the identical name.
    """
    raw = (sid or "default").encode("utf-8", "replace")
    return re.sub(rb"[^A-Za-z0-9_-]", b"_", raw).decode("ascii")[:128]


def _muted_dir() -> Path:
    return paths.control_dir() / "muted"


def is_muted(session_id) -> bool:
    try:
        return (_muted_dir() / safe_sid(session_id)).exists()
    except Exception:
        return False


def mute(sids) -> None:
    d = _muted_dir()
    d.mkdir(parents=True, exist_ok=True)
    for sid in sids:
        f = d / safe_sid(sid)
        if not f.exists():
            f.write_text(json.dumps({"since": time.time()}), encoding="utf-8")


def unmute(sids) -> None:
    for sid in sids:
        try:
            (_muted_dir() / safe_sid(sid)).unlink()
        except FileNotFoundError:
            pass


def muted() -> list:
    try:
        return sorted(p.name for p in _muted_dir().iterdir() if p.is_file())
    except OSError:
        return []


def _paused_path() -> Path:
    return paths.control_dir() / "paused"


def is_paused() -> bool:
    try:
        return _paused_path().exists()
    except Exception:
        return False


MOODS = {"quiet": {"categories": ("error", "final_answer"), "length": 0.5, "tools": 0},
         "normal": {"categories": ("error", "final_answer", "insight", "status"), "length": 1.0, "tools": 0},
         "chatty": {"categories": ("error", "final_answer", "insight", "status"), "length": 1.5, "tools": 1},
         "narrate": {"categories": ("error", "final_answer", "insight", "status"), "length": 2.0, "tools": 2}}


def _mood_path() -> Path:
    return paths.control_dir() / "mood"


def mood() -> str:
    """Current mood; one small read per event, never cached. Unset or unreadable = normal."""
    try:
        name = _mood_path().read_text(encoding="utf-8").strip()
    except Exception:
        return "normal"
    return name if name in MOODS else "normal"


def set_mood(name: str) -> str:
    if name not in MOODS:
        raise ValueError(f"unknown mood: {name!r} (expected one of {', '.join(MOODS)})")
    paths.control_dir().mkdir(parents=True, exist_ok=True)
    _mood_path().write_text(name, encoding="utf-8")
    return name


def pause() -> None:
    """Global pause: set the flag, then best-effort cut what is playing now (daemon down is fine)."""
    d = paths.control_dir()
    d.mkdir(parents=True, exist_ok=True)
    if not is_paused():
        _paused_path().write_text(json.dumps({"since": time.time()}), encoding="utf-8")
    from daemon import service_ctl
    service_ctl.socket_request({"command": "interrupt"})


def resume() -> None:
    try:
        _paused_path().unlink()
    except FileNotFoundError:
        pass


DICTATION_HOLD_MAX_S = 150  # Claude Code caps a recording at 2 min; a crashed helper's flag expires


def dictating() -> bool:
    """True while a dictation tool records: playback holds, nothing is dropped."""
    try:
        return time.time() - (paths.control_dir() / "dictating").stat().st_mtime < DICTATION_HOLD_MAX_S
    except OSError:
        return False


# --- rules -------------------------------------------------------------------
@contextmanager
def locked(name: str):
    """Exclusive advisory lock on ``control_dir()/<name>`` for load-modify-save sections."""
    d = paths.control_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / name, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _rules_path() -> Path:
    return paths.control_dir() / "voices.json"


def load_rules() -> dict:
    """Rules dict, re-read only when voices.json's (mtime, size) changes."""
    p = _rules_path()
    try:
        st = p.stat()
    except OSError:
        _RULES_CACHE.update(sig=None, rules={})
        return {}
    sig = (str(p), st.st_mtime_ns, st.st_size)
    if _RULES_CACHE["sig"] != sig:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            raw = data.get("rules") if isinstance(data, dict) else None
        except (OSError, ValueError):
            raw = None
        rules = {}
        for k, v in (raw if isinstance(raw, dict) else {}).items():
            entry = _clean_entry(k, v)
            if entry:
                rules[k] = entry
        _RULES_CACHE.update(sig=sig, rules=rules)
    return _RULES_CACHE["rules"]


def _coerce(field: str, value):
    """Validated value for ``field`` (numbers clamped into range); ValueError if unusable."""
    if field not in FIELDS:
        raise ValueError(f"unknown field: {field!r} (expected one of {', '.join(FIELDS)})")
    kind, lo, hi = FIELDS[field]
    if kind is str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field}: expected a voice name, got {value!r}")
        return value
    try:
        x = float(value) if not isinstance(value, bool) else math.nan
    except (TypeError, ValueError, OverflowError):  # a 400-digit int must not break every command
        x = math.nan
    if not math.isfinite(x) or (kind is int and not x.is_integer()):
        raise ValueError(f"{field}: expected {'an integer' if kind is int else 'a number'} {lo}-{hi}, got {value!r}")
    return min(max(kind(x), lo), hi)


def _clean_entry(rule_key, entry) -> dict:
    """The valid fields of one stored rule (+ set_at); invalid fields are skipped, not fatal."""
    if not isinstance(entry, dict):
        return {}
    out = {}
    for field in FIELDS:
        if field not in entry or (field == "voice" and str(rule_key).startswith("voice:")):
            continue
        try:
            out[field] = _coerce(field, entry[field])
        except ValueError:
            pass
    if out and isinstance(entry.get("set_at"), (int, float)):
        out["set_at"] = entry["set_at"]
    return out


def _save_rules(rules: dict) -> None:
    p = _rules_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps({"version": 2, "rules": rules}, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _normalize_key(scope: str, key: str) -> str:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope!r} (expected one of {', '.join(SCOPES)})")
    if scope == "all":
        return "*"
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be non-empty")
    return key


def set_rule(scope: str, key: str, value, field: str = "voice") -> dict:
    """Set one field of the ``<scope>:<key>`` rule, keeping its other fields."""
    key = _normalize_key(scope, key)
    value = _coerce(field, value)
    if scope == "voice" and field == "voice":
        raise ValueError("a voice-scope rule cannot set the voice field")
    if field == "voice" or scope == "voice":
        catalog = voices_info()["catalog"]
        for v in (value if field == "voice" else None, key if scope == "voice" else None):
            if v is not None and v not in catalog:
                raise ValueError(f"unknown voice: {v!r}")
    with locked("voices.lock"):
        rules = dict(load_rules())
        rule_key = f"{scope}:{key}"
        rules[rule_key] = {**{k: v for k, v in rules.get(rule_key, {}).items() if k != "set_at"},
                           field: value, "set_at": time.time()}
        _save_rules(rules)
    return rules


def clear_rule(scope: str, key: str, field: Optional[str] = None) -> dict:
    """Clear one field of a rule (the entry goes with its last field), or the whole rule."""
    key = _normalize_key(scope, key)
    if field is not None and field not in FIELDS:
        raise ValueError(f"unknown field: {field!r} (expected one of {', '.join(FIELDS)})")
    with locked("voices.lock"):
        rules = dict(load_rules())
        rule_key = f"{scope}:{key}"
        entry = {k: v for k, v in rules.get(rule_key, {}).items() if k != field}
        if field is None or not set(entry) & set(FIELDS):
            rules.pop(rule_key, None)
        else:
            rules[rule_key] = entry
        _save_rules(rules)
    return rules


# --- identity ----------------------------------------------------------------
@functools.lru_cache(maxsize=1024)
def _true_case(path: str) -> str:
    """`path` spelled as the directory entries spell it.

    realpath resolves links but keeps the caller's case, so on a case-insensitive
    volume `~/GitHub/x` and `~/GITHUB/x` would be two projects with two rule keys.
    """
    parts = []
    head = path
    while True:
        parent, name = os.path.split(head)
        if not name:
            break
        try:
            entries = os.listdir(parent or ".")
        except OSError:
            entries = []
        if name not in entries:
            name = next((e for e in entries if e.casefold() == name.casefold()), name)
        parts.append(name)
        head = parent
    return os.path.join(head, *reversed(parts))


def _identify_uncached(cwd: str) -> dict:
    real = _true_case(os.path.realpath(cwd))
    ident = {"cwd": cwd, "workspace": os.path.dirname(real), "project": real,
             "project_name": os.path.basename(real), "worktree": real,
             "branch": None, "is_git": False}
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel", "--git-common-dir",
             "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout.splitlines()
        top, common, branch = out[0], out[1], out[2]
    except (OSError, subprocess.SubprocessError, IndexError):
        return ident
    common = os.path.abspath(os.path.join(cwd, common))
    # only a plain repo's common dir is <project>/.git; submodule/bare common dirs are not under a project folder
    project = os.path.dirname(common) if os.path.basename(common) == ".git" else (
        top if "/.git/modules/" in common.replace(os.sep, "/") else common)
    project = _true_case(os.path.realpath(project))
    ident.update(worktree=_true_case(os.path.realpath(top)), project=project,
                 project_name=os.path.basename(project),
                 workspace=os.path.dirname(project),
                 branch=None if branch == "HEAD" else branch, is_git=True)
    return ident


def identify(cwd: str) -> dict:
    """Project identity for a cwd; cached 30 s per cwd."""
    now = _clock()
    hit = _IDENT_CACHE.get(cwd)
    if hit and now - hit[0] < IDENTIFY_TTL_S:
        return hit[1]
    ident = _identify_uncached(cwd)
    if len(_IDENT_CACHE) >= _IDENTIFY_MAX:
        _IDENT_CACHE.clear()
    _IDENT_CACHE[cwd] = (now, ident)
    return ident


def _rule_keys(session_id: str, ident: Optional[dict], voice: Optional[str] = None) -> list:
    """(scope, key) candidates, narrowest first."""
    keys = [("session", session_id)]
    if ident:
        if ident["branch"]:
            keys.append(("branch", f"{ident['project']}@{ident['branch']}"))
        keys += [("worktree", ident["worktree"]), ("project", ident["project"]),
                 ("workspace", ident["workspace"])]
    if voice:
        keys.append(("voice", voice))
    keys.append(("all", "*"))
    return keys


def _lookup(field: str, session_id, cwd, voice: Optional[str] = None):
    """(value, scope, key) of the narrowest rule setting ``field``, or None.

    Git runs only if a git-scope rule sets the field; the session's voice is
    resolved (or ``voice`` used) only if a voice-scope rule sets it.
    """
    rules = load_rules()
    scopes = {k.split(":", 1)[0] for k, v in rules.items() if field in v}
    if not scopes:
        return None
    ident = identify(cwd) if cwd and scopes & set(_GIT_SCOPES) else None
    if "voice" in scopes and voice is None:
        voice = _session_voice(session_id, cwd, voices_info())[0]
    for scope, key in _rule_keys(session_id, ident, voice if "voice" in scopes else None):
        rule = rules.get(f"{scope}:{key}")
        if rule and field in rule:
            return rule[field], scope, key
    return None


def resolve_setting(field: str, session_id, cwd):
    """(value, source_scope) from the narrowest rule setting ``field``; (None, "default") if none.

    Raises ValueError only for an unknown field; anything else fails soft to the default.
    """
    if field not in FIELDS:
        raise ValueError(f"unknown field: {field!r} (expected one of {', '.join(FIELDS)})")
    try:
        hit = _lookup(field, session_id, cwd)
    except Exception:
        hit = None
    return (hit[0], hit[1]) if hit else (None, "default")


def override_voice(session_id, cwd):
    """(voice, scope, key) of the narrowest rule setting the voice, or None. Never raises."""
    try:
        return _lookup("voice", session_id, cwd)
    except Exception:
        return None


# --- config / pool -----------------------------------------------------------
def load_config() -> dict:
    """Active config, same search order as TTSDaemon._load_tts_user_config."""
    for p in paths.config_candidates():
        if p.exists():  # first existing file wins: falling through would show a stale config as live
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    return cfg
                return {"config_error": f"{p}: not a JSON object"}
            except (OSError, ValueError) as e:
                return {"config_error": f"{p}: {e}"}
    return {}


def voices_info(cfg: Optional[dict] = None) -> dict:
    cfg = load_config() if cfg is None else cfg
    voice = cfg.get("voice") if isinstance(cfg.get("voice"), dict) else {}
    pv = cfg.get("project_voices") if isinstance(cfg.get("project_voices"), dict) else {}
    engine, default = voice.get("engine"), voice.get("name")
    enabled = bool(pv.get("enabled", False))
    pool = [v.strip() for v in pv.get("pool") or [] if isinstance(v, str) and v.strip()] if enabled else []
    names = [n for genders in config_io.VOICE_CATALOGS.get(engine, {}).values() for n in genders]
    catalog = list(dict.fromkeys(names + pool + ([default] if default else [])))
    return {"engine": engine, "default_voice": default, "rate": voice.get("rate"),
            "volume": voice.get("volume"), "pool": pool, "pool_enabled": enabled,
            "catalog": catalog, **({"config_error": cfg["config_error"]} if "config_error" in cfg else {})}


def config_state(restart_needed: bool = False) -> dict:
    """The editable slice of the active config (daemon defaults where unset) for the panel."""
    cfg = load_config()
    info = voices_info(cfg)
    voice = cfg.get("voice") if isinstance(cfg.get("voice"), dict) else {}
    pv = cfg.get("project_voices") if isinstance(cfg.get("project_voices"), dict) else {}
    out = {"path": str(config_io.active_config_path()), "config_error": cfg.get("config_error"),
           "voice": {"engine": info["engine"] or "edge-tts", "name": info["default_voice"] or "en-US-AvaNeural",
                     "rate": voice.get("rate", 1.0), "volume": voice.get("volume", 1.0)},
           "project_voices": {"enabled": info["pool_enabled"], "pool": pv.get("pool") or []},
           "voices": info["catalog"]}
    return {**out, "restart_needed": True} if restart_needed else out


def _config_value(key: str, raw: str):
    """The CLI string as the type update_config expects (pool is a JSON array, enabled true/false)."""
    if key == "voice.name":
        return raw
    if key == "project_voices.enabled":
        if raw not in ("true", "false"):
            raise ValueError(f"{key}: expected true or false, got {raw!r}")
        return raw == "true"
    try:
        return json.loads(raw)
    except ValueError:
        raise ValueError(f"{key}: expected a JSON value, got {raw!r}") from None


def _project_label(cwd: str) -> str:
    """Copy of ContentRouter._project_root + _project_label (parity-tested)."""
    if not isinstance(cwd, str) or not cwd.strip():
        return ""
    root = cwd = cwd.strip()
    try:
        d = os.path.abspath(cwd)
        while True:
            if os.path.exists(os.path.join(d, ".git")):
                root = d
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    except Exception:
        root = cwd
    proj = root.strip().rstrip("/").rsplit("/", 1)[-1]
    return "" if proj in ("", "/", ".", "tmp") else proj


def pool_voice(cwd, info: Optional[dict] = None) -> Optional[str]:
    info = voices_info() if info is None else info
    proj = _project_label(cwd)
    if not info["pool_enabled"] or not info["pool"] or not proj:
        return None
    return info["pool"][int(hashlib.sha256(proj.encode("utf-8")).hexdigest(), 16) % len(info["pool"])]


def _session_voice(session_id, cwd, info: dict) -> tuple:
    """(voice, source, rule_key): voice rule, else hashed project pool, else config default."""
    hit = override_voice(session_id, cwd)
    if hit:
        return hit
    if (pooled := pool_voice(cwd, info)):
        return pooled, "project_pool", None
    return info["default_voice"], "default", None


def resolve(session_id, cwd, info: Optional[dict] = None) -> dict:
    info = voices_info() if info is None else info
    voice, source, rule_key = _session_voice(session_id, cwd, info)
    effective = {"voice": {"value": voice, "source": source}}
    for field in FIELDS:
        if field != "voice":
            try:
                hit = _lookup(field, session_id, cwd, voice=voice)
            except Exception:
                hit = None
            effective[field] = {"value": hit[0], "source": hit[1]} if hit else {"value": None, "source": "default"}
    return {"session_id": session_id, "voice": voice, "source": source,
            "rule_key": rule_key, "engine": info["engine"], "rate": info["rate"],
            "volume": info["volume"], "muted": is_muted(session_id), "effective": effective}


# --- notification prefs ------------------------------------------------------
DISK_ALERT_MODES = ("popup", "log", "off")
DISK_ALERT_EVERY_S = (60, 900, 3600, 86400)
NOTIFICATION_KINDS = ("permission_prompt", "idle_prompt", "other")
_PREFS_CACHE: dict = {"sig": None, "prefs": None}


def _default_prefs() -> dict:
    return {"disk_alert": "popup", "disk_alert_every_s": 60,
            "speak": {k: True for k in NOTIFICATION_KINDS}}


def _prefs_path() -> Path:
    return paths.control_dir() / "notifications.json"


def _merge_prefs(raw) -> dict:
    """Defaults overlaid with whichever fields of ``raw`` are valid."""
    prefs = _default_prefs()
    if not isinstance(raw, dict):
        return prefs
    if raw.get("disk_alert") in DISK_ALERT_MODES:
        prefs["disk_alert"] = raw["disk_alert"]
    every = raw.get("disk_alert_every_s")
    if type(every) is int and every in DISK_ALERT_EVERY_S:
        prefs["disk_alert_every_s"] = every
    speak = raw.get("speak")
    if isinstance(speak, dict):
        for k in NOTIFICATION_KINDS:
            if isinstance(speak.get(k), bool):
                prefs["speak"][k] = speak[k]
    return prefs


def notification_prefs() -> dict:
    """Merged notification prefs; re-read only when the file's (mtime, size) changes. Never raises."""
    try:
        p = _prefs_path()
        st = p.stat()
        sig = (str(p), st.st_mtime_ns, st.st_size)
        if _PREFS_CACHE["sig"] != sig:
            _PREFS_CACHE.update(sig=sig, prefs=_merge_prefs(json.loads(p.read_text(encoding="utf-8"))))
        return json.loads(json.dumps(_PREFS_CACHE["prefs"]))
    except Exception:
        return _default_prefs()


def set_notification_prefs(updates: dict) -> dict:
    """Apply dotted-key string updates (all validated first), write atomically, return prefs."""
    with locked("notifications.lock"):
        return _set_notification_prefs(updates)


def _set_notification_prefs(updates: dict) -> dict:
    prefs = notification_prefs()
    for key, value in updates.items():
        if key == "disk_alert":
            if value not in DISK_ALERT_MODES:
                raise ValueError(f"{key}: expected one of {', '.join(DISK_ALERT_MODES)}, got {value!r}")
            prefs[key] = value
        elif key == "disk_alert_every_s":
            if not str(value).isdigit() or int(value) not in DISK_ALERT_EVERY_S:
                raise ValueError(f"{key}: expected one of {', '.join(map(str, DISK_ALERT_EVERY_S))}, got {value!r}")
            prefs[key] = int(value)
        elif key.startswith("speak.") and key[6:] in NOTIFICATION_KINDS:
            if value not in ("true", "false"):
                raise ValueError(f"{key}: expected true or false, got {value!r}")
            prefs["speak"][key[6:]] = value == "true"
        else:
            raise ValueError(f"unknown preference key: {key!r}")
    p = _prefs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return prefs


def notification_kind(payload_or_message) -> str:
    """permission_prompt | idle_prompt | other. Mirrored with jq in hooks/speech_output_hook.sh."""
    if isinstance(payload_or_message, dict):
        t, msg = payload_or_message.get("notification_type"), payload_or_message.get("message")
        if t in ("permission_prompt", "idle_prompt"):
            return t
    else:
        msg = payload_or_message
    msg = msg.lower() if isinstance(msg, str) else ""
    if "permission" in msg:
        return "permission_prompt"
    if "waiting for your input" in msg or "waiting for input" in msg:
        return "idle_prompt"
    return "other"


def allow_notification(kind) -> bool:
    return notification_prefs()["speak"].get(kind, True)


# --- CLI ---------------------------------------------------------------------
def _snapshot(stdin_text: str) -> dict:
    sessions = json.loads(stdin_text or "{}").get("sessions") or []
    cwds = sorted({s["cwd"] for s in sessions if isinstance(s.get("cwd"), str) and s["cwd"]})
    with ThreadPoolExecutor(max_workers=8) as ex:
        identities = dict(zip(cwds, ex.map(identify, cwds)))
    info = voices_info()
    return {"config": info, "muted": muted(), "paused": is_paused(), "mood": mood(), "rules": load_rules(), "identities": identities,
            "resolved": {s["session_id"]: resolve(s["session_id"], s.get("cwd"), info)
                         for s in sessions}}


def _flag(argv: list, name: str) -> str:
    if name not in argv or argv.index(name) + 1 >= len(argv):
        raise ValueError(f"missing {name}")
    return argv[argv.index(name) + 1]


def _opt(argv: list, name: str, default: str) -> str:
    return _flag(argv, name) if name in argv else default


def main(argv: list) -> int:
    try:
        cmd, args = (argv[0] if argv else ""), argv[1:]
        if cmd == "snapshot":
            out = _snapshot(sys.stdin.read())
        elif cmd in ("pause", "resume"):
            (pause if cmd == "pause" else resume)()
            out = {"paused": is_paused()}
        elif cmd == "mood":
            out = {"mood": set_mood(_flag(args, "--set")) if "--set" in args else mood()}
        elif cmd in ("daemon-status", "daemon-start", "daemon-stop", "daemon-restart"):
            from daemon import service_ctl
            try:
                out = service_ctl.run(cmd.split("-", 1)[1])
            except service_ctl.Failed as exc:  # contract: exit 1 + one-line stderr reason
                print(exc, file=sys.stderr)
                return 1
        elif cmd in ("mute", "unmute"):
            (mute if cmd == "mute" else unmute)(args)
            out = {"muted": muted()}
        elif cmd == "set-voice":
            out = {"rules": set_rule(_flag(args, "--scope"), _flag(args, "--key"), _flag(args, "--voice"))}
        elif cmd == "clear-voice":
            out = {"rules": clear_rule(_flag(args, "--scope"), _flag(args, "--key"), "voice")}
        elif cmd == "set-rule":
            out = {"rules": set_rule(_flag(args, "--scope"), _flag(args, "--target"),
                                     _flag(args, "--value"), _flag(args, "--field"))}
        elif cmd == "clear-rule":
            out = {"rules": clear_rule(_flag(args, "--scope"), _flag(args, "--target"), _flag(args, "--field"))}
        elif cmd == "config-get":
            out = config_state()
        elif cmd == "config-set":
            key = _flag(args, "--key")
            config_io.update_config(key, _config_value(key, _flag(args, "--value")))
            out = config_state(restart_needed=True)
        elif cmd == "identify":
            out = {c: identify(c) for c in args}
        elif cmd == "feedback":
            from daemon import feedback
            text, cat = _flag(args, "--text"), _flag(args, "--category")
            if "--line" in args:  # per-line rating; without it the legacy stacked --label record
                rec = feedback.vote(_flag(args, "--line"), _flag(args, "--axis"), _flag(args, "--value"),
                                    _flag(args, "--sid"), _flag(args, "--cwd"), cat, text)
            else:
                rec = feedback.record(
                    _flag(args, "--label"), _flag(args, "--sid"), _flag(args, "--cwd"), cat, text,
                    float(_opt(args, "--weight", "1.0")), _opt(args, "--source", "panel"))
            out = {"recorded": rec, "effect": feedback.adjust(rec["project"], cat, text)}
        elif cmd == "votes":
            from daemon import feedback
            out = {"votes": feedback.votes([i for i in _flag(args, "--lines").split(",") if i])}
        elif cmd == "implicit-pause":
            from daemon import feedback
            out = {"recorded": feedback.implicit_pause(_flag(args, "--sid"), _flag(args, "--cwd"))}
        elif cmd == "tuning":
            from daemon import feedback
            out = feedback.tuning()
        elif cmd == "forget":
            from daemon import feedback
            feedback.forget(_flag(args, "--project"), _flag(args, "--signature"), _opt(args, "--category", ""))
            out = {"ok": True}
        elif cmd == "notifications":
            updates = {}
            if args and (len(args) % 2 or any(a != "--set" for a in args[::2])):
                raise ValueError("usage: notifications [--set KEY=VALUE ...]")
            for item in args[1::2]:
                k, sep, v = item.partition("=")
                if not sep:
                    raise ValueError(f"{item!r}: expected KEY=VALUE")
                updates[k] = v
            out = {"prefs": set_notification_prefs(updates) if updates else notification_prefs()}
        else:
            raise ValueError("usage: session_control.py snapshot|pause|resume|daemon-status|daemon-start|daemon-stop|daemon-restart|mute|unmute|set-voice|clear-voice|set-rule|clear-rule|config-get|config-set|identify|mood|feedback|votes|implicit-pause|tuning|forget|notifications")
    except Exception as exc:  # contract: every failure is {"error": ...} + exit 2
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
