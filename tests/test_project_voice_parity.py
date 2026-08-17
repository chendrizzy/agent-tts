"""Parity gate: the `/tts:voices` + `/tts:log` surfaces vs the real daemon.

`commands/voices.md` and `commands/log.md` each embed an INLINE copy of the
daemon's deterministic project->voice mapping, because an installed slash command
has to be self-contained (it cannot import `daemon.content_router`). An index
that disagrees with what is actually spoken is worse than no index, so this test
pins the reproduced mapping against the REAL ``ContentRouter`` functions.

Two layers:
  1. behavioural — the reproduction below must equal the daemon for real repo
     roots, subdirectories, and the no-project cases.
  2. textual — the shipped command files must still contain the exact hash
     expression, so editing the inline copy fails this gate.

Hermetic: repo roots are built under ``tmp_path``; no absolute personal paths
(the plugin PII gate in tests/test_plugin_manifest.py forbids them).

# ponytail: the ideal cleanup is a shared voice-map helper the router and the
# surfaces both read; deferred while the mapping is only ~10 lines.
"""

import hashlib
import os

import pytest

from daemon.content_router import ContentRouter

POOL = ["af_heart", "am_michael", "bf_emma", "am_fenrir", "af_bella", "bm_george"]

# The exact expression the inline copies use. Textual drift = failing test.
HASH_EXPR = 'int(hashlib.sha256('


# --- the reproduction embedded in the command files ---------------------------
def _root(cwd):
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


def _label(cwd):
    if not isinstance(cwd, str):
        return ""
    c = cwd.strip()
    if not c:
        return ""
    proj = c.rstrip("/").rsplit("/", 1)[-1]
    if not proj or proj in ("", "/", ".", "tmp"):
        return ""
    return proj


def _listing_voice(cwd, pool, enabled=True):
    """What the /tts:voices listing and /tts:log tag compute for a cwd."""
    if not enabled or not pool:
        return None
    proj = _label(_root(cwd))
    if not proj:
        return None
    idx = int(hashlib.sha256(proj.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[idx]


# --- the daemon's ground truth (real functions, read-only) --------------------
def _daemon_voice(router, cwd):
    """Exactly what the running daemon assigns for a session cwd."""
    return router._resolve_project_voice(router._project_label(router._project_root(cwd)))


@pytest.fixture
def router():
    return ContentRouter(config={"project_voices": {"enabled": True, "pool": POOL}})


@pytest.fixture
def repos(tmp_path):
    """Two real repo roots plus a nested subdir, built on disk."""
    for name in ("alpha-service", "beta_ui"):
        (tmp_path / name / ".git").mkdir(parents=True)
    (tmp_path / "alpha-service" / "src" / "deep").mkdir(parents=True)
    return tmp_path


def test_listing_matches_daemon_for_repo_roots(router, repos):
    """Listing voice == daemon voice for every real repo root."""
    for name in ("alpha-service", "beta_ui"):
        cwd = str(repos / name)
        assert _daemon_voice(router, cwd) is not None
        assert _listing_voice(cwd, POOL) == _daemon_voice(router, cwd)


def test_subdir_collapses_to_repo_voice(router, repos):
    """A subdirectory of a repo gets the SAME voice as the repo root."""
    root = str(repos / "alpha-service")
    sub = str(repos / "alpha-service" / "src" / "deep")
    expected = _daemon_voice(router, root)
    assert expected is not None
    assert _daemon_voice(router, sub) == expected
    assert _listing_voice(sub, POOL) == expected


def test_distinct_repos_resolve_independently(router, repos):
    """Each repo resolves through the same hash the daemon uses."""
    for name in ("alpha-service", "beta_ui"):
        label = name
        idx = int(hashlib.sha256(label.encode("utf-8")).hexdigest(), 16) % len(POOL)
        assert _daemon_voice(router, str(repos / name)) == POOL[idx]


def test_generic_root_has_no_voice(router):
    """/tmp and blank cwd resolve to no project => no per-project voice."""
    assert _daemon_voice(router, "/tmp") is None
    assert _listing_voice("/tmp", POOL) is None
    assert _listing_voice("", POOL) is None


def test_disabled_or_empty_pool_yields_none(repos):
    """Faithful to _resolve_project_voice's disabled/empty guards."""
    cwd = str(repos / "alpha-service")
    assert _listing_voice(cwd, POOL, enabled=False) is None
    assert _listing_voice(cwd, []) is None
    off = ContentRouter(config={"project_voices": {"enabled": False, "pool": POOL}})
    assert _daemon_voice(off, cwd) is None
    empty = ContentRouter(config={"project_voices": {"enabled": True, "pool": []}})
    assert _daemon_voice(empty, cwd) is None


def test_cwdless_event_still_resolves_a_voice(router, repos, monkeypatch):
    """REGRESSION: an event with no `cwd` must resolve via the session-cwd map.

    _project_for_event falls back to spoken_log.get_session_cwd() inside a
    best-effort `except Exception`. When that accessor did not exist in this
    tree the fallback raised AttributeError, was swallowed, and EVERY cwd-less
    event (the stop/prose path — exactly the turn-final speech users hear)
    silently resolved to no project, so no per-project voice applied. The
    swallowing except means a missing accessor cannot fail loudly; only this
    test can.
    """
    from daemon import spoken_log

    root = str(repos / "alpha-service")
    spoken_log.note_session_cwd("sess-cwdless", root)
    assert spoken_log.get_session_cwd("sess-cwdless") == root

    voice = _daemon_voice(router, root)
    assert voice is not None
    assert router._resolve_project_voice(
        router._project_for_event({"session_id": "sess-cwdless"})) == voice


@pytest.mark.parametrize("command", ["voices.md", "log.md"])
def test_command_files_still_embed_the_mapping(command):
    """Textual pin: editing the inline copy must fail this gate."""
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "commands" / command).read_text(
        encoding="utf-8")
    assert HASH_EXPR in text, f"{command}: inline project->voice hash is gone"
    assert "% len(" in text, f"{command}: inline pool modulo is gone"
