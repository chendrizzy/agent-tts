---
name: tts:voices
description: Show the deterministic project → voice map (which repo sounds like which voice), current repo highlighted.
---

# /tts:voices — project → voice index

Per-project voices are audibly distinct but otherwise invisible. This prints the
daemon's deterministic `project → voice` map so you can see, at a glance, which
repo speaks in which voice. The current repo is highlighted with `▶`.

The mapping is the daemon's own: a project is its REPO ROOT (nearest `.git`
ancestor), and the voice is `pool[ sha256(basename(root)) % len(pool) ]` — the
exact logic in `daemon/content_router.py`
(`_project_root` → `_project_label` → `_resolve_project_voice`). The pool and
enable flags are read from your config — `$CLAUDE_TTS_CONFIG`, else
`~/.config/claude-tts/config.json` (honoring `$XDG_CONFIG_HOME`). Restart the
daemon to apply config changes.

"Known/active repos" = repos the daemon has actually spoken in (distinct cwds in
`~/.claude/logs/tts/spoken/*.jsonl`), plus the current repo, plus any repo paths
you pass as arguments.

Run:

```bash
ARGS="$ARGUMENTS"
python3 - "$PWD" $ARGS <<'PY'
# Reproduces daemon/content_router.py's project->voice mapping EXACTLY. Kept
# inline so the command stays self-contained as an installed plugin. Pinned to
# the daemon by tests/test_project_voice_parity.py (imports the real functions).
import sys, os, json, hashlib, glob

CWD = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
EXTRA = [a for a in sys.argv[2:] if a.strip()]

# --- config: pool + enable flags ----------------------------------------------
# Mirrors daemon/paths.py config_path() precedence: $CLAUDE_TTS_CONFIG, then
# $XDG_CONFIG_HOME/claude-tts/config.json (else ~/.config/claude-tts/config.json).
# The trailing entries are legacy source-tree layouts, kept last as fallbacks.
def load_config():
    cands = []
    if os.environ.get("CLAUDE_TTS_CONFIG"):
        cands.append(os.environ["CLAUDE_TTS_CONFIG"])
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    cands.append(os.path.join(xdg, "claude-tts", "config.json"))
    cands.append(os.path.expanduser("~/.claude/tts/config/tts_user_config.json"))
    cands.append(os.path.join(CWD, "config", "tts_user_config.json"))
    for c in cands:
        try:
            with open(c, encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            continue
    return {}

cfg = load_config()
pv = cfg.get("project_voices", {}) if isinstance(cfg, dict) else {}
# Daemon defaults: enabled False, pool [] (content_router.py ctor).
pv_enabled = bool(pv.get("enabled", False))
pool = list(pv.get("pool", []) or [])
default_voice = ((cfg.get("voice", {}) or {}).get("name")) or "(engine default)"
# Per-project voice assignment is independent of output_context; output_context
# only controls verbal project/context phrasing.
active = pv_enabled and bool(pool)

# --- the daemon's mapping (root -> label -> voice) ----------------------------
def project_root(cwd):
    if not isinstance(cwd, str) or not cwd.strip():
        return ""
    cwd = cwd.strip()
    root = cwd
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
    return root

def project_label(cwd):
    if not isinstance(cwd, str):
        return ""
    c = cwd.strip()
    if not c:
        return ""
    proj = c.rstrip("/").rsplit("/", 1)[-1]
    if not proj or proj in ("", "/", ".", "tmp"):
        return ""
    return proj

def voice_for(label):
    if not pool or not label:
        return None
    idx = int(hashlib.sha256(label.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[idx]

# --- gather known/active repo roots -------------------------------------------
def spoken_cwds():
    out = set()
    for f in glob.glob(os.path.expanduser("~/.claude/logs/tts/spoken/*.jsonl")):
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        r = json.loads(ln)
                    except Exception:
                        continue
                    c = r.get("cwd")
                    if c:
                        out.add(c)
        except OSError:
            pass
    return out

cur_root = project_root(CWD)
roots = {}  # root -> label
for c in list(spoken_cwds()) + EXTRA + [CWD]:
    r = project_root(c)
    lbl = project_label(r)
    if lbl:
        roots[r] = lbl

# --- render -------------------------------------------------------------------
print("# /tts:voices — project → voice")
if not active:
    why = []
    if not pv_enabled:
        why.append("project_voices.enabled=false")
    if not pool:
        why.append("empty pool")
    print(f"per-project voices are OFF ({', '.join(why) or 'disabled'}); "
          f"the daemon speaks everything in the default voice: {default_voice}")
    print("(showing the mapping that WOULD apply if enabled)")
print(f"pool ({len(pool)}): {', '.join(pool) if pool else '(none)'}")
print(f"default voice: {default_voice}")
print()

if not roots:
    print("(no known repos yet — pass a repo path, or speak something first)")
    raise SystemExit

width = max((len(l) for l in roots.values()), default=7)
for root in sorted(roots, key=lambda r: roots[r].lower()):
    lbl = roots[root]
    voice = voice_for(lbl) or "-"
    mark = "▶" if root == cur_root else " "
    print(f"{mark} {lbl:<{width}}  ·  {voice:<10}  {root}")
PY
```
