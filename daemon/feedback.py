"""Feedback-driven tuning of what the router speaks, how urgently, how condensed.

Stdlib-only leaf. The panel records ratings of spoken lines (``record``); the
router asks ``adjust`` on every decision. Ratings are keyed contextually by
(project, category, utterance template) and decay exponentially so the system
keeps adapting instead of freezing on old opinions.

State: ``paths.control_dir()/feedback.jsonl``, append-only, one record per line:
    {"ts", "sid", "project", "category", "signature",
     "label": useful|noise|urgent|verbose|reset, "weight", "source", "text"}
Implicit records also carry ``uts`` (the utterance's timestamp) so a line is voted on once.
A ``reset`` record zeroes every earlier record with the same (project, signature);
signature "*" zeroes the (project, category) instead (empty category: the project).
"""
import json
import os
import re
import time
from typing import Optional

from daemon import paths, session_control, spoken_log

LABELS = ("useful", "noise", "urgent", "verbose", "reset")
HALF_LIFE_S = 14 * 86400.0       # old opinions fade: preferences drift
IMPLICIT_WINDOW_S = 90.0         # a pause this soon after speech weakly implies "not wanted"
IMPLICIT_WEIGHT = 0.3            # implicit evidence is weaker than an explicit click
URGENT_SCORE = 1.5               # urgent counts more than useful: missing it costs more
SUPPRESS_SCORE = -1.5            # two explicit noise votes mute a template
SUPPRESS_SCORE_ERROR = -3.0      # errors are expensive to miss: demand more evidence
URGENT_MIN = 0.5                 # decayed urgent mass needed for the template boost
CATEGORY_DELTA_MAX = 3           # category-level drift is gentle
CATEGORY_DELTA_SCALE = 3.0       # full agreement among many votes approaches the max
TEMPLATE_URGENT_BOOST = 3        # an urgent-rated template jumps the queue
DELTA_MAX = 5                    # combined bound before the router clamps priority to 1..10
GLOBAL_FALLBACK = 0.5            # other projects' votes on a template count half
VERBOSE_BASE = 0.7               # each decayed verbose vote shrinks the summarize threshold by 30%
VERBOSE_FLOOR = 0.35             # never condense below a third of the threshold
TUNING_MAX_TEMPLATES = 200
COMPACT_BYTES = 2 * 1024 * 1024  # record() compacts the log past this size
COMPACT_MIN_WEIGHT = 1e-3        # decayed weight below this carries no signal

_CACHE: dict = {"sig": None, "by_sig": {}, "by_cat": {}, "all": [], "tmpl_any": {}}

_QUOTED = re.compile(r"\"[^\"]*\"|'[^'\s][^']*'(?=\W|$)")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b")
_PATH = re.compile(r"(?<![\w/])/[\w.~@+-]+(?:/[\w.~@+-]*)*")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
# Router output-context prefixes ("In proj, " / "In target: ") are not part of the template.
_PREFIX = re.compile(r"^in [^,:]*[,:]\s+")
_TOKEN = re.compile(r"<path>|<hex>|\"…\"|#|[^\W\d_]+(?:'[^\W\d_]+)?")


def signature(text: str) -> str:
    """Template of an utterance so recurring router phrasings group together.

    "In my-proj, 12 tests passed in /a/b.py"  -> "# tests passed in <path>"
    "Build 4f3a9c2e1b failed after 3.5 seconds" -> "build <hex> failed after # seconds"
    'Found "foo bar" in 7 files'              -> 'found "…" in # files'
    """
    s = _PREFIX.sub("", (text or "").lower().strip())
    s = _QUOTED.sub('"…"', s)
    s = _UUID.sub("<hex>", s)
    s = _HEX.sub("<hex>", s)
    s = _PATH.sub("<path>", s)
    s = _NUM.sub("#", s)
    return " ".join(_TOKEN.findall(s)[:8])


def _path():
    return paths.control_dir() / "feedback.jsonl"


def has_data() -> bool:
    return _path().exists()


# --- store -------------------------------------------------------------------
def _append(rec: dict, compact: bool = False) -> None:
    p = _path()
    with session_control.locked("feedback.lock"):
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if compact or p.stat().st_size > COMPACT_BYTES:
            _compact(p)


def _reduce(lines) -> list:
    """Valid records after reset processing, in file order."""
    recs: list = []
    for line in lines:
        try:
            r = json.loads(line)
            if r["label"] not in LABELS:
                continue
            float(r["ts"]), float(r["weight"])
        except (ValueError, KeyError, TypeError):
            continue
        if r["label"] == "reset":
            if r.get("signature") == "*":
                recs = [x for x in recs if not (x["project"] == r["project"] and (
                    not r.get("category") or x["category"] == r["category"]))]
            else:
                recs = [x for x in recs
                        if not (x["project"] == r["project"] and x["signature"] == r["signature"])]
        else:
            recs.append(r)
    return recs


def _compact(p) -> None:
    """Rewrite the log without reset-zeroed or fully-decayed records (caller holds the lock)."""
    now = time.time()
    keep = [r for r in _reduce(p.read_text(encoding="utf-8", errors="ignore").splitlines())
            if float(r["weight"]) * 0.5 ** (max(0.0, now - float(r["ts"])) / HALF_LIFE_S) >= COMPACT_MIN_WEIGHT]
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
    os.replace(tmp, p)


def _project(cwd) -> str:
    return session_control.identify(cwd)["project"] if cwd else ""


def record(label: str, sid: str, cwd, category: str, text: str,
           weight: float = 1.0, source: str = "panel", uts: Optional[float] = None) -> dict:
    if label not in LABELS:
        raise ValueError(f"unknown label: {label!r} (expected one of {', '.join(LABELS)})")
    if source not in ("panel", "implicit"):
        raise ValueError(f"unknown source: {source!r}")
    text = text or ""
    rec = {"ts": time.time(), "sid": sid, "project": _project(cwd), "category": category or "other",
           "signature": signature(text), "label": label, "weight": float(weight),
           "source": source, "text": text[:300]}
    if uts is not None:
        rec["uts"] = uts
    _append(rec)
    return rec


def forget(project: str, signature_: str, category: str = "") -> dict:
    rec = {"ts": time.time(), "sid": "", "project": project, "category": category,
           "signature": signature_, "label": "reset", "weight": 1.0, "source": "panel", "text": ""}
    _append(rec, compact=True)
    return rec


def implicit_pause(sid: str, cwd, now: Optional[float] = None) -> int:
    """Weak noise votes for what this session spoke in the last IMPLICIT_WINDOW_S.

    At most one implicit vote per (session, utterance) ever: repeated pauses over the
    same lines must not accumulate into a verdict."""
    now = time.time() if now is None else now
    try:
        lines = spoken_log.session_path(sid).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return 0
    voted: set = set()
    try:
        for line in _path().read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                r = json.loads(line)
                if r.get("source") == "implicit" and r.get("sid") == sid and "uts" in r:
                    voted.add(float(r["uts"]))
            except (ValueError, TypeError, AttributeError):
                continue
    except OSError:
        pass
    n = 0
    for line in lines:
        try:
            e = json.loads(line)
            uts = float(e["ts"])
            if now - IMPLICIT_WINDOW_S <= uts <= now and e.get("text") and uts not in voted:
                record("noise", sid, cwd, e.get("category") or "other", e["text"],
                       IMPLICIT_WEIGHT, "implicit", uts=uts)
                voted.add(uts)
                n += 1
        except (ValueError, KeyError, TypeError):
            continue
    return n


def _load() -> dict:
    """Records after reset processing, indexed; re-read only when the file changes."""
    p = _path()
    st = p.stat()
    sig = (str(p), st.st_mtime_ns, st.st_size)
    if _CACHE["sig"] == sig:
        return _CACHE
    recs = _reduce(p.read_text(encoding="utf-8", errors="ignore").splitlines())
    by_sig: dict = {}
    by_cat: dict = {}
    any_sig: dict = {}
    for r in recs:
        by_sig.setdefault((r["project"], r["signature"]), []).append(r)
        by_cat.setdefault((r["project"], r["category"]), []).append(r)
        any_sig.setdefault(r["signature"], []).append(r)
    _CACHE.update(sig=sig, by_sig=by_sig, by_cat=by_cat, all=recs, tmpl_any=any_sig)
    return _CACHE


# --- scoring -----------------------------------------------------------------
def _tally(recs: list, now: float, scale: float = 1.0) -> dict:
    t = {"useful": 0.0, "noise": 0.0, "urgent": 0.0, "verbose": 0.0, "noise_explicit": 0.0}
    for r in recs:
        w = scale * float(r["weight"]) * 0.5 ** (max(0.0, now - float(r["ts"])) / HALF_LIFE_S)
        t[r["label"]] += w
        if r["label"] == "noise" and r.get("source") != "implicit":
            t["noise_explicit"] += w
    t["n"] = t["useful"] + t["noise"] + t["urgent"]
    t["score"] = t["useful"] + URGENT_SCORE * t["urgent"] - t["noise"]
    return t


def _suppressed(t: dict, category: str) -> bool:
    limit = SUPPRESS_SCORE_ERROR if category == "error" else SUPPRESS_SCORE
    # implicit pauses alone never mute a template: they may only lower category priority
    return (t["score"] <= limit and t["noise"] > t["useful"] + t["urgent"]
            and t["noise_explicit"] > 0)


def _category_delta(t: dict) -> int:
    d = round(CATEGORY_DELTA_SCALE * t["score"] / (t["n"] + 2))
    return max(-CATEGORY_DELTA_MAX, min(CATEGORY_DELTA_MAX, d))


def _factor(verbose: float) -> float:
    return max(VERBOSE_FLOOR, VERBOSE_BASE ** verbose)


def _neutral(sig: str) -> dict:
    return {"suppress": False, "reason": "", "priority_delta": 0, "summarize_factor": 1.0, "signature": sig}


def adjust(project: str, category: str, content: str, now: Optional[float] = None) -> dict:
    """Router-facing verdict for one decision. Never raises."""
    sig = signature(content)
    try:
        if not has_data():
            return _neutral(sig)
        now = time.time() if now is None else now
        c = _load()
        own = c["by_sig"].get((project, sig))
        tmpl = (_tally(own, now) if own else _tally(c["tmpl_any"].get(sig, []), now, GLOBAL_FALLBACK)) \
            if sig else _tally([], now)
        cat = _tally(c["by_cat"].get((project, category), []), now)
        out = _neutral(sig)
        if sig and _suppressed(tmpl, category):  # no signature (CJK/emoji-only...) -> no template verdict
            out.update(suppress=True, reason=f"template '{sig}' score {tmpl['score']:.1f}")
        delta = _category_delta(cat)
        if sig and tmpl["urgent"] >= URGENT_MIN:
            delta += TEMPLATE_URGENT_BOOST
        out["priority_delta"] = max(-DELTA_MAX, min(DELTA_MAX, delta))
        out["summarize_factor"] = _factor(cat["verbose"] + tmpl["verbose"])
        return out
    except Exception:
        return _neutral(sig)


def tuning(now: Optional[float] = None) -> dict:
    """What has been learned, for the panel: per-template and per-category state."""
    now = time.time() if now is None else now
    if not has_data():
        return {"templates": [], "categories": []}
    c = _load()
    templates = []
    for (project, sig), recs in c["by_sig"].items():
        t = _tally(recs, now)
        last = max(recs, key=lambda r: r["ts"])
        state = ("suppressed" if sig and _suppressed(t, last["category"])
                 else "boosted" if t["urgent"] >= URGENT_MIN or t["score"] >= 1.5 else "neutral")
        templates.append({"project": project, "signature": sig, "score": round(t["score"], 3),
                          "useful": round(t["useful"], 3), "noise": round(t["noise"], 3),
                          "urgent": round(t["urgent"], 3), "verbose": round(t["verbose"], 3),
                          "state": state, "example": last["text"]})
    templates.sort(key=lambda x: -abs(x["score"]))
    categories = []
    for (project, category), recs in c["by_cat"].items():
        t = _tally(recs, now)
        categories.append({"project": project, "category": category,
                           "priority_delta": _category_delta(t),
                           "summarize_factor": _factor(t["verbose"]), "n": round(t["n"], 3)})
    return {"templates": templates[:TUNING_MAX_TEMPLATES], "categories": categories}
