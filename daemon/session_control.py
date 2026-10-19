"""Per-session control layer: mute flags and scoped voice-override rules.

Stdlib-only leaf. Importable as ``daemon.session_control`` (daemon + hooks) and
runnable as ``python3 daemon/session_control.py <cmd>`` (the panel's CLI; every
command prints ONE JSON document on stdout, errors as ``{"error": msg}`` / exit 2).

State lives under ``paths.control_dir()``:
    muted/<safe_sid>   flag file, content ``{"since": <epoch>}``
    voices.json        ``{"version": 1, "rules": {"<scope>:<key>": {"voice", "set_at"}}}``

Rule scopes, narrowest first: session, branch, worktree, project, workspace, all.
The daemon calls override_voice() on every event, so it must be cheap and must
never raise: no git subprocess unless a git-derived-scope rule actually exists.
"""
import sys

if __name__ == "__main__":  # script mode: repo root (not daemon/) must lead sys.path
    import os as _os
    sys.path[0] = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

import fcntl  # noqa: E402
import functools  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from daemon import config_io, paths  # noqa: E402

SCOPES = ("session", "branch", "worktree", "project", "workspace", "all")
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
        _RULES_CACHE.update(sig=sig, rules={
            k: v for k, v in (raw if isinstance(raw, dict) else {}).items()
            if isinstance(v, dict) and isinstance(v.get("voice"), str)
        })
    return _RULES_CACHE["rules"]


def _save_rules(rules: dict) -> None:
    p = _rules_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps({"version": 1, "rules": rules}, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _normalize_key(scope: str, key: str) -> str:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope!r} (expected one of {', '.join(SCOPES)})")
    if scope == "all":
        return "*"
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be non-empty")
    return key


def set_rule(scope: str, key: str, voice: str) -> dict:
    key = _normalize_key(scope, key)
    if voice not in voices_info()["catalog"]:
        raise ValueError(f"unknown voice: {voice!r}")
    with locked("voices.lock"):
        rules = dict(load_rules())
        rules[f"{scope}:{key}"] = {"voice": voice, "set_at": time.time()}
        _save_rules(rules)
    return rules


def clear_rule(scope: str, key: str) -> dict:
    key = _normalize_key(scope, key)
    with locked("voices.lock"):
        rules = dict(load_rules())
        rules.pop(f"{scope}:{key}", None)
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
    project = _true_case(os.path.realpath(os.path.dirname(os.path.abspath(os.path.join(cwd, common)))))
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


def _rule_keys(session_id: str, ident: Optional[dict]) -> list:
    """(scope, key) candidates, narrowest first."""
    keys = [("session", session_id)]
    if ident:
        if ident["branch"]:
            keys.append(("branch", f"{ident['project']}@{ident['branch']}"))
        keys += [("worktree", ident["worktree"]), ("project", ident["project"]),
                 ("workspace", ident["workspace"])]
    keys.append(("all", "*"))
    return keys


def override_voice(session_id, cwd):
    """(voice, scope, key) of the narrowest matching rule, or None. Never raises."""
    try:
        rules = load_rules()
        if not rules:
            return None
        need_git = bool(cwd) and any(k.split(":", 1)[0] in _GIT_SCOPES for k in rules)
        ident = identify(cwd) if need_git else None
        for scope, key in _rule_keys(session_id, ident):
            rule = rules.get(f"{scope}:{key}")
            if rule:
                return rule["voice"], scope, key
    except Exception:
        pass
    return None


# --- config / pool -----------------------------------------------------------
def load_config() -> dict:
    """Active config, same search order as TTSDaemon._load_tts_user_config."""
    for p in (paths.config_path(),
              Path(__file__).resolve().parent.parent / "config" / "tts_user_config.json",
              Path.home() / ".claude" / "tts" / "config" / "tts_user_config.json"):
        try:
            if p.exists():
                cfg = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    return cfg
        except (OSError, ValueError):
            continue
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
            "catalog": catalog}


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


def resolve(session_id, cwd, info: Optional[dict] = None) -> dict:
    info = voices_info() if info is None else info
    hit = override_voice(session_id, cwd)
    if hit:
        voice, source, rule_key = hit
    elif (pooled := pool_voice(cwd, info)):
        voice, source, rule_key = pooled, "project_pool", None
    else:
        voice, source, rule_key = info["default_voice"], "default", None
    return {"session_id": session_id, "voice": voice, "source": source,
            "rule_key": rule_key, "engine": info["engine"], "rate": info["rate"],
            "volume": info["volume"], "muted": is_muted(session_id)}


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
    return {"config": info, "muted": muted(), "rules": load_rules(), "identities": identities,
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
        elif cmd in ("mute", "unmute"):
            (mute if cmd == "mute" else unmute)(args)
            out = {"muted": muted()}
        elif cmd == "set-voice":
            out = {"rules": set_rule(_flag(args, "--scope"), _flag(args, "--key"), _flag(args, "--voice"))}
        elif cmd == "clear-voice":
            out = {"rules": clear_rule(_flag(args, "--scope"), _flag(args, "--key"))}
        elif cmd == "identify":
            out = {c: identify(c) for c in args}
        elif cmd == "feedback":
            from daemon import feedback
            text, cat = _flag(args, "--text"), _flag(args, "--category")
            rec = feedback.record(
                _flag(args, "--label"), _flag(args, "--sid"), _flag(args, "--cwd"), cat, text,
                float(_opt(args, "--weight", "1.0")), _opt(args, "--source", "panel"))
            out = {"recorded": rec, "effect": feedback.adjust(rec["project"], cat, text)}
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
            raise ValueError("usage: session_control.py snapshot|mute|unmute|set-voice|clear-voice|identify|feedback|implicit-pause|tuning|forget|notifications")
    except Exception as exc:  # contract: every failure is {"error": ...} + exit 2
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
