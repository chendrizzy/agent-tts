"""Per-session control layer: mute flags, scoped voice rules, identity, CLI, wiring.

Hermetic: every test points AGENT_TTS_CONTROL_DIR at tmp_path and builds its own
git repos under tmp_path. Nothing touches ~/.claude or a running daemon.
"""
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daemon import session_control as sc  # noqa: E402
from daemon.content_router import ContentRouter  # noqa: E402
from daemon.tts_types import Category, RouterDecision  # noqa: E402

HOOKS = ROOT / "hooks"
CLI = ROOT / "daemon" / "session_control.py"
POOL = ["af_heart", "am_michael", "bf_emma", "am_fenrir", "af_bella", "bm_george"]
CFG = {
    "voice": {"engine": "kokoro", "name": "af_heart", "rate": 1.0, "volume": 1.0},
    "project_voices": {"enabled": True, "pool": POOL},
}


@pytest.fixture(autouse=True)
def ctl(tmp_path, monkeypatch):
    d = tmp_path / "ctl"
    monkeypatch.setenv("AGENT_TTS_CONTROL_DIR", str(d))
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(CFG))
    monkeypatch.setenv("CLAUDE_TTS_CONFIG", str(cfg))
    sc._IDENT_CACHE.clear()
    sc._RULES_CACHE.update(sig=None, rules={})
    return d


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "ws" / "proj"
    r.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _git(r, "commit", "-q", "--allow-empty", "-m", "init")
    return r


# --- identify ----------------------------------------------------------------
def test_identify_git_repo(repo):
    sub = repo / "src"
    sub.mkdir()
    i = sc.identify(str(sub))
    real = os.path.realpath(repo)
    assert i == {"cwd": str(sub), "workspace": os.path.dirname(real), "project": real,
                 "project_name": "proj", "worktree": real, "branch": "main", "is_git": True}


def test_identify_spells_paths_as_the_disk_does(repo):
    # On a case-insensitive volume another spelling of the same folder must give the
    # same keys, or one project's sessions split across two sets of voice rules.
    variant = os.path.join(os.path.dirname(str(repo)), os.path.basename(str(repo)).upper())
    same = sc.identify(str(repo))
    if os.path.exists(variant):
        other = sc.identify(variant)
        assert (other["project"], other["worktree"], other["workspace"]) == (same["project"], same["worktree"], same["workspace"])
    assert sc._true_case(os.path.realpath(str(repo))) == os.path.realpath(str(repo))


def test_identify_linked_worktree_shares_project(repo, tmp_path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    i = sc.identify(str(wt))
    assert i["project"] == os.path.realpath(repo)
    assert i["worktree"] == os.path.realpath(wt)
    assert i["branch"] == "feat" and i["is_git"]
    assert i["workspace"] == os.path.dirname(os.path.realpath(repo))


def test_identify_detached_head_has_no_branch(repo):
    _git(repo, "checkout", "-q", "--detach")
    assert sc.identify(str(repo))["branch"] is None


def test_identify_non_git(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    i = sc.identify(str(d))
    real = os.path.realpath(d)
    assert i["is_git"] is False and i["branch"] is None
    assert i["project"] == i["worktree"] == real
    assert i["workspace"] == os.path.dirname(real)


def test_identify_cache_ttl(repo, monkeypatch):
    t = [100.0]
    monkeypatch.setattr(sc, "_clock", lambda: t[0])
    calls = []
    real = sc._identify_uncached
    monkeypatch.setattr(sc, "_identify_uncached", lambda c: calls.append(c) or real(c))
    sc.identify(str(repo))
    t[0] += 29
    sc.identify(str(repo))
    assert len(calls) == 1
    t[0] += 2
    sc.identify(str(repo))
    assert len(calls) == 2


# --- rules / precedence ------------------------------------------------------
def test_precedence_narrowest_wins(repo):
    cwd, sid = str(repo), "sess-1"
    i = sc.identify(cwd)
    ladder = [
        ("all", "*", "v_all"),
        ("workspace", i["workspace"], "v_ws"),
        ("project", i["project"], "v_proj"),
        ("worktree", i["worktree"], "v_wt"),
        ("branch", f"{i['project']}@main", "v_br"),
        ("session", sid, "v_sess"),
    ]
    with patch.object(sc, "voices_info", return_value={"catalog": [v for *_, v in ladder]}):
        for scope, key, voice in ladder:
            sc.set_rule(scope, key, voice)
            assert sc.override_voice(sid, cwd) == (voice, scope, "*" if scope == "all" else key)


def test_other_session_not_matched_by_session_rule(repo):
    sc.set_rule("session", "a", "af_bella")
    assert sc.override_voice("b", str(repo)) is None


def test_fast_path_runs_no_git(monkeypatch):
    sc.set_rule("session", "s", "af_bella")
    sc.set_rule("all", "ignored", "bf_emma")
    boom = MagicMock(side_effect=AssertionError("git ran"))
    monkeypatch.setattr(subprocess, "run", boom)
    assert sc.override_voice("s", "/some/cwd") == ("af_bella", "session", "s")
    assert sc.override_voice("other", "/some/cwd") == ("bf_emma", "all", "*")
    boom.assert_not_called()


def test_override_never_raises(monkeypatch):
    sc.set_rule("project", "/p", "af_bella")
    monkeypatch.setattr(sc, "identify", MagicMock(side_effect=RuntimeError("x")))
    assert sc.override_voice("s", "/p") is None


def test_rules_reload_on_external_edit(ctl):
    sc.set_rule("session", "s", "af_bella")
    assert sc.override_voice("s", None)[0] == "af_bella"
    p = ctl / "voices.json"
    data = json.loads(p.read_text())
    data["rules"]["session:s"]["voice"] = "bm_george"
    p.write_text(json.dumps(data, indent=4))  # size changes -> cache invalidates
    assert sc.override_voice("s", None)[0] == "bm_george"


def test_set_rule_validation():
    with pytest.raises(ValueError, match="voice"):
        sc.set_rule("session", "s", "not-a-voice")
    with pytest.raises(ValueError, match="scope"):
        sc.set_rule("galaxy", "s", "af_bella")
    with pytest.raises(ValueError, match="key"):
        sc.set_rule("project", "  ", "af_bella")
    assert "all:*" in sc.set_rule("all", "whatever", "af_bella")


def test_clear_rule():
    sc.set_rule("session", "s", "af_bella")
    assert sc.clear_rule("session", "s") == {}
    assert sc.override_voice("s", None) is None


# --- mute --------------------------------------------------------------------
def test_mute_unmute(ctl):
    assert not sc.is_muted("a/b")
    sc.mute(["a/b", "c"])
    assert sc.is_muted("a/b") and sc.muted() == ["a_b", "c"]
    assert json.loads((ctl / "muted" / "c").read_text())["since"] > 0
    sc.unmute(["a/b", "never-muted"])
    assert sc.muted() == ["c"]


def test_dictating_flag_holds_and_expires(ctl):
    assert not sc.dictating()
    ctl.mkdir(parents=True, exist_ok=True)
    flag = ctl / "dictating"
    flag.write_text("")
    assert sc.dictating()
    stale = flag.stat().st_mtime - sc.DICTATION_HOLD_MAX_S - 1
    os.utime(flag, (stale, stale))  # a crashed helper's flag must not silence TTS forever
    assert not sc.dictating()


def test_safe_sid():
    assert sc.safe_sid("ok-1_X") == "ok-1_X"
    assert sc.safe_sid("../etc/pw d") == "___etc_pw_d"
    assert sc.safe_sid("é") == "__"  # byte-wise, like `tr -c` under LC_ALL=C
    assert sc.safe_sid("a" * 300) == "a" * 128
    assert sc.safe_sid("") == sc.safe_sid(None) == "default"


# --- config / pool / resolve -------------------------------------------------
def test_voices_info():
    info = sc.voices_info()
    assert info["engine"] == "kokoro" and info["default_voice"] == "af_heart"
    assert info["pool_enabled"] and info["pool"] == POOL
    assert {"af_heart", "bm_george", "am_fenrir"} <= set(info["catalog"])
    assert len(info["catalog"]) == len(set(info["catalog"]))


@pytest.mark.parametrize("label", ["alpha-service", "beta_ui", "x", "Çafé"])
def test_pool_voice_equals_content_router(tmp_path, label):
    (tmp_path / label / ".git").mkdir(parents=True)
    (tmp_path / label / "src").mkdir()
    router = ContentRouter(config=CFG)
    for cwd in (str(tmp_path / label), str(tmp_path / label / "src")):
        want = router._resolve_project_voice(router._project_label(router._project_root(cwd)))
        assert want is not None
        assert sc.pool_voice(cwd) == want


def test_pool_voice_none_cases(tmp_path):
    assert sc.pool_voice("") is None
    assert sc.pool_voice(str(tmp_path), {"pool_enabled": False, "pool": POOL}) is None


def test_resolve_sources(repo):
    cwd = str(repo)
    r = sc.resolve("s", cwd)
    assert (r["source"], r["voice"]) == ("project_pool", sc.pool_voice(cwd))
    sc.set_rule("project", sc.identify(cwd)["project"], "bm_george")
    r = sc.resolve("s", cwd)
    assert (r["source"], r["voice"]) == ("project", "bm_george")
    assert r["rule_key"] == sc.identify(cwd)["project"] and r["muted"] is False
    sc.mute(["s"])
    assert sc.resolve("s", cwd)["muted"] is True


def test_resolve_default_when_pool_disabled(tmp_path, monkeypatch):
    cfg = tmp_path / "c2.json"
    cfg.write_text(json.dumps({"voice": CFG["voice"]}))
    monkeypatch.setenv("CLAUDE_TTS_CONFIG", str(cfg))
    r = sc.resolve("s", str(tmp_path))
    assert (r["source"], r["voice"], r["rule_key"]) == ("default", "af_heart", None)


# --- CLI ---------------------------------------------------------------------
def _cli(*args, stdin="", env=None):
    p = subprocess.run([sys.executable, str(CLI), *args], input=stdin, text=True,
                       capture_output=True, env={**os.environ, **(env or {})})
    return p.returncode, json.loads(p.stdout)


def test_cli_snapshot_set_clear_mute(repo):
    cwd = str(repo)
    sessions = {"sessions": [{"session_id": "s1", "cwd": cwd}, {"session_id": "s2", "cwd": cwd},
                             {"session_id": "s3", "cwd": None}]}
    rc, out = _cli("snapshot", stdin=json.dumps(sessions))
    assert rc == 0 and set(out) == {"config", "muted", "paused", "rules", "identities", "resolved"}
    assert out["identities"][cwd]["branch"] == "main" and list(out["identities"]) == [cwd]
    assert out["resolved"]["s1"]["source"] == "project_pool" and set(out["resolved"]) == {"s1", "s2", "s3"}

    rc, out = _cli("set-voice", "--scope", "session", "--key", "s1", "--voice", "bm_george")
    assert rc == 0 and out["rules"]["session:s1"]["voice"] == "bm_george"
    assert _cli("mute", "s1", "s2") == (0, {"muted": ["s1", "s2"]})
    assert _cli("unmute", "s2") == (0, {"muted": ["s1"]})
    rc, out = _cli("snapshot", stdin=json.dumps(sessions))
    assert out["resolved"]["s1"]["source"] == "session" and out["resolved"]["s1"]["muted"] is True
    assert out["resolved"]["s2"]["muted"] is False and out["muted"] == ["s1"]
    assert _cli("clear-voice", "--scope", "session", "--key", "s1") == (0, {"rules": {}})


def test_cli_invalid_voice_and_usage_errors():
    rc, out = _cli("set-voice", "--scope", "session", "--key", "s", "--voice", "nope")
    assert rc == 2 and "voice" in out["error"]
    assert _cli("set-voice", "--scope", "session", "--key", "s")[0] == 2
    assert _cli("set-voice", "--scope", "bogus", "--key", "s", "--voice", "af_heart")[0] == 2
    assert _cli("bogus")[0] == 2
    assert _cli("snapshot", stdin="{not json")[0] == 2


def test_cli_identify(repo, tmp_path):
    rc, out = _cli("identify", str(repo), str(tmp_path))
    assert rc == 0 and out[str(repo)]["is_git"] and not out[str(tmp_path)]["is_git"]


def test_cli_snapshot_100_sessions_fast(tmp_path):
    cwds = []
    for n in range(40):
        d = tmp_path / f"p{n}"
        d.mkdir()
        cwds.append(str(d))
    sessions = {"sessions": [{"session_id": f"s{n}", "cwd": cwds[n % 40]} for n in range(100)]}
    import time
    t0 = time.monotonic()
    rc, out = _cli("snapshot", stdin=json.dumps(sessions))
    assert rc == 0 and len(out["resolved"]) == 100 and len(out["identities"]) == 40
    assert time.monotonic() - t0 < 5


# --- daemon wiring -----------------------------------------------------------
def _daemon():
    from daemon.tts_daemon import TTSDaemon
    d = TTSDaemon.__new__(TTSDaemon)
    d.logger = logging.getLogger("tts_daemon_test_session_control")
    d.log = lambda msg, level="INFO": None
    d.stats = {"requests_received": 0}
    return d


@pytest.mark.parametrize("command", ["tool_event", "stop_event"])
def test_muted_session_event_is_acked_and_dropped(command):
    sc.mute(["sess-m"])
    d = _daemon()
    d._handle_tool_event = d._handle_stop_event = MagicMock(side_effect=AssertionError("routed"))
    sent = []
    sock = MagicMock()
    sock.send.side_effect = sent.append
    payload = {"command": command, "session_id": "sess-m", "event_id": "e1"}
    with patch("daemon.tts_daemon._recv_line", return_value=(json.dumps(payload) + "\n").encode()):
        d.handle_client(sock)
    assert json.loads(sent[0]) == {"status": "ok", "event_id": "e1", "queued": False, "reason": "muted"}


def test_override_voice_lands_in_router_decision(tmp_path):
    router = ContentRouter(config={})  # no project pool configured
    dec = RouterDecision(should_speak=True, category=Category.STATUS, content="hi", priority=5,
                         source_event_id="e", classified_at=0.0)
    event = {"session_id": "sess-v", "cwd": str(tmp_path)}
    assert router._apply_project_voice(dec, event).voice is None
    sc.set_rule("session", "sess-v", "bm_george")
    assert router._apply_project_voice(dec, event).voice == "bm_george"


def test_playback_loop_drops_items_of_muted_session():
    import asyncio
    from daemon.pipeline.playback_stage import PlaybackStage
    src = (ROOT / "daemon" / "pipeline" / "playback_stage.py").read_text()
    assert "session_control.is_muted(session_id)" in src
    assert PlaybackStage._playback_loop  # guard sits between buffer.get() and _play_audio
    assert src.index("session_control.is_muted(session_id)") < src.index("await self._play_audio(")
    assert asyncio  # (full loop needs live afplay; covered structurally above)


# --- hooks -------------------------------------------------------------------
import socket as socketlib  # noqa: E402
import tempfile  # noqa: E402


def _run_hook_with_listener(tmp_path, script, payload, sid):
    sock_dir = tempfile.mkdtemp(dir="/tmp")  # AF_UNIX paths are length-limited
    srv = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
    srv.bind(os.path.join(sock_dir, "s.sock"))
    srv.listen(4)
    srv.settimeout(1.5)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    p = subprocess.run(
        ["bash", str(HOOKS / script)],
        input=json.dumps({**payload, "session_id": sid, "conversation_id": sid}).encode(),
        capture_output=True, timeout=30,
        env={**os.environ, "HOME": str(home), "CLAUDE_TTS_SOCKET": os.path.join(sock_dir, "s.sock"),
             "CLAUDE_TTS_ENABLED": "true"},
    )
    return p, srv, home


def test_pre_tool_hook_does_contact_socket_when_not_muted(tmp_path):
    """Control for the muted cases: same hook + sid, no flag -> it connects."""
    p, srv, _ = _run_hook_with_listener(
        tmp_path, "pre-tool-use.sh",
        {"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"question": "Pick?"}]}},
        "weird sid/é:1",
    )
    conn, _ = srv.accept()
    conn.close()
    srv.close()
    assert p.returncode == 0

@pytest.mark.parametrize("script,payload", [
    ("post-tool-use.sh", {"tool_name": "Bash", "tool_input": {"command": "ls"},
                           "tool_response": {"stdout": "x"}}),
    ("pre-tool-use.sh", {"tool_name": "AskUserQuestion",
                          "tool_input": {"questions": [{"question": "Pick?"}]}}),
    ("speech_output_hook.sh", {"transcript_path": "", "stop_hook_active": False}),
    ("cursor-pre-tool-use.sh", {"tool_name": "Shell", "tool_input": {"command": "ls"}}),
    ("cursor-post-tool-use.sh", {"tool_name": "Shell", "tool_input": {"command": "ls"}}),
])
def test_hooks_exit_without_contacting_socket_when_muted(tmp_path, script, payload):
    sid = "weird sid/é:1"
    sc.mute([sid])
    p, srv, home = _run_hook_with_listener(tmp_path, script, payload, sid)
    assert p.returncode == 0
    with pytest.raises(socketlib.timeout):
        srv.accept()  # nothing connected
    srv.close()
    if script in ("post-tool-use.sh", "speech_output_hook.sh"):
        logs = "".join(f.read_text() for f in (home / ".claude/logs/hooks").glob("*.log"))
        assert "session muted" in logs


@pytest.mark.parametrize("script", ["cursor-pre-tool-use.sh", "cursor-post-tool-use.sh"])
def test_cursor_hooks_mute_by_session_id_before_conversation_id(tmp_path, script):
    """cursor_normalize.py sends session_id (else conversation_id) to the daemon; the mute check must match."""
    sc.mute(["sess-A"])
    sock_dir = tempfile.mkdtemp(dir="/tmp")
    srv = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
    srv.bind(os.path.join(sock_dir, "s.sock"))
    srv.listen(4)
    srv.settimeout(1.5)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    p = subprocess.run(
        ["bash", str(HOOKS / script)],
        input=json.dumps({"tool_name": "Shell", "tool_input": {"command": "ls"},
                          "session_id": "sess-A", "conversation_id": "conv-B"}).encode(),
        capture_output=True, timeout=30,
        env={**os.environ, "HOME": str(home), "CLAUDE_TTS_SOCKET": os.path.join(sock_dir, "s.sock"),
             "CLAUDE_TTS_ENABLED": "true"},
    )
    assert p.returncode == 0
    with pytest.raises(socketlib.timeout):
        srv.accept()
    srv.close()


def test_agent_response_normalizer_prefers_session_id():
    sys.path.insert(0, str(HOOKS))
    import cursor_normalize as cn
    assert cn.normalize_agent_response({"session_id": "A", "conversation_id": "B", "text": "x"})["session_id"] == "A"
    assert cn.normalize_tool_hook({"session_id": "A", "conversation_id": "B"}, "pre")["session_id"] == "A"
    assert cn.normalize_agent_response({"conversation_id": "B", "text": "x"})["session_id"] == "B"


def test_voice_rule_writes_do_not_lose_updates(tmp_path):
    import multiprocessing as mp
    ctx = mp.get_context("fork")
    voice = next(iter(sc.voices_info()["catalog"]))

    def worker(i):
        sc.set_rule("session", f"s{i}", voice)
    ps = [ctx.Process(target=worker, args=(i,)) for i in range(12)]
    [p.start() for p in ps]
    [p.join() for p in ps]
    assert {f"session:s{i}" for i in range(12)} <= set(sc.load_rules())


# --- multi-field rules (voices.json v2) ----------------------------------------
def test_v1_file_reads_unchanged(ctl):
    ctl.mkdir(parents=True, exist_ok=True)
    (ctl / "voices.json").write_text(json.dumps({"version": 1, "rules": {
        "session:s": {"voice": "bm_george", "set_at": 1.0}, "all:*": {"voice": "af_bella", "set_at": 2.0},
        "session:junk": {"set_at": 3.0}}}))
    assert sc.load_rules() == {"session:s": {"voice": "bm_george", "set_at": 1.0},
                               "all:*": {"voice": "af_bella", "set_at": 2.0}}
    assert sc.override_voice("s", None) == ("bm_george", "session", "s")
    assert sc.override_voice("x", None) == ("af_bella", "all", "*")
    sc.set_rule("session", "t", 0.5, "volume")  # any write upgrades the file
    assert json.loads((ctl / "voices.json").read_text())["version"] == 2


def test_fields_fall_through_independently(repo):
    cwd = str(repo)
    i = sc.identify(cwd)
    sc.set_rule("branch", f"{i['project']}@main", 1.5, "volume")
    sc.set_rule("project", i["project"], "bm_george")
    sc.set_rule("all", "*", 0.2, "volume")
    assert sc.resolve_setting("volume", "s", cwd) == (1.5, "branch")
    assert sc.resolve_setting("voice", "s", cwd) == ("bm_george", "project")
    assert sc.resolve_setting("rate", "s", cwd) == (None, "default")
    eff = sc.resolve("s", cwd)["effective"]
    assert eff == {"voice": {"value": "bm_george", "source": "project"},
                   "volume": {"value": 1.5, "source": "branch"},
                   "rate": {"value": None, "source": "default"},
                   "frequency": {"value": None, "source": "default"},
                   "verbosity": {"value": None, "source": "default"}}
    with pytest.raises(ValueError, match="field"):
        sc.resolve_setting("pitch", "s", cwd)


def test_voice_scope_below_workspace_above_all(repo):
    cwd = str(repo)
    i = sc.identify(cwd)
    sc.set_rule("session", "s", "bm_george")
    sc.set_rule("all", "*", 1, "verbosity")
    sc.set_rule("voice", "bm_george", 3, "verbosity")
    sc.set_rule("voice", "af_bella", 5, "verbosity")  # another voice: never matches
    assert sc.resolve_setting("verbosity", "s", cwd) == (3, "voice")
    assert sc.resolve_setting("verbosity", "other", cwd) == (1, "all")  # pool voice, no rule for it
    sc.set_rule("workspace", i["workspace"], 4, "verbosity")
    assert sc.resolve_setting("verbosity", "s", cwd) == (4, "workspace")
    # the pool / default voice also keys voice-scope rules
    sc.set_rule("voice", sc.pool_voice(cwd), 2, "frequency")
    assert sc.resolve_setting("frequency", "other", cwd) == (2, "voice")
    assert sc.resolve("other", cwd)["effective"]["frequency"] == {"value": 2, "source": "voice"}


def test_voice_scope_cannot_set_voice_and_needs_known_voice():
    with pytest.raises(ValueError, match="voice-scope"):
        sc.set_rule("voice", "bm_george", "af_bella")
    with pytest.raises(ValueError, match="unknown voice"):
        sc.set_rule("voice", "nobody", 1.0, "rate")


def test_voice_scope_voice_field_ignored_on_read(ctl):
    ctl.mkdir(parents=True, exist_ok=True)
    (ctl / "voices.json").write_text(json.dumps({"version": 2, "rules": {
        "voice:bm_george": {"voice": "af_bella", "rate": 1.2}}}))
    assert sc.load_rules() == {"voice:bm_george": {"rate": 1.2}}


def test_field_validation_and_clamping():
    assert sc.set_rule("session", "s", "9", "volume")["session:s"]["volume"] == 2.0
    assert sc.set_rule("session", "s", -1, "volume")["session:s"]["volume"] == 0.0
    assert sc.set_rule("session", "s", "0.1", "rate")["session:s"]["rate"] == 0.5
    assert sc.set_rule("session", "s", "7", "frequency")["session:s"]["frequency"] == 5
    assert sc.set_rule("session", "s", 0, "verbosity")["session:s"]["verbosity"] == 1
    rule = sc.load_rules()["session:s"]
    assert type(rule["frequency"]) is int and type(rule["volume"]) is float
    for field, bad in [("frequency", "2.5"), ("volume", "loud"), ("rate", "nan"), ("verbosity", True),
                       ("volume", None)]:
        with pytest.raises(ValueError, match=field):
            sc.set_rule("session", "s", bad, field)
    with pytest.raises(ValueError, match="field"):
        sc.set_rule("session", "s", 1, "pitch")


def test_invalid_field_ignored_not_whole_rule(ctl):
    ctl.mkdir(parents=True, exist_ok=True)
    (ctl / "voices.json").write_text(json.dumps({"version": 2, "rules": {
        "session:s": {"voice": "bm_george", "volume": "loud", "rate": 9, "frequency": 2.5, "set_at": 1.0},
        "session:t": {"volume": "loud", "pitch": 3},
        "session:u": {"voice": "bm_george", "volume": 10 ** 400}}}))
    assert sc.load_rules() == {"session:s": {"voice": "bm_george", "rate": 2.0, "set_at": 1.0}, "session:u": {"voice": "bm_george"}}


def test_set_keeps_other_fields_clear_removes_one():
    sc.set_rule("session", "s", "bm_george")
    sc.set_rule("session", "s", 0.7, "volume")
    assert {k: v for k, v in sc.load_rules()["session:s"].items() if k != "set_at"} == {"voice": "bm_george", "volume": 0.7}
    rules = sc.clear_rule("session", "s", "voice")
    assert "voice" not in rules["session:s"] and rules["session:s"]["volume"] == 0.7
    assert sc.override_voice("s", None) is None
    assert sc.clear_rule("session", "s", "volume") == {}


def test_cli_set_rule_clear_rule_round_trip(repo):
    cwd = str(repo)
    rc, out = _cli("set-rule", "--scope", "project", "--target", sc.identify(cwd)["project"],
                   "--field", "volume", "--value", "3")
    assert rc == 0 and out["rules"][f"project:{sc.identify(cwd)['project']}"]["volume"] == 2.0
    rc, out = _cli("set-rule", "--scope", "voice", "--target", "bm_george", "--field", "rate", "--value", "1.25")
    assert rc == 0 and out["rules"]["voice:bm_george"]["rate"] == 1.25
    rc, out = _cli("snapshot", stdin=json.dumps({"sessions": [{"session_id": "s1", "cwd": cwd}]}))
    assert out["resolved"]["s1"]["effective"]["volume"] == {"value": 2.0, "source": "project"}
    assert out["rules"]["voice:bm_george"]["rate"] == 1.25
    assert _cli("set-rule", "--scope", "voice", "--target", "bm_george", "--field", "voice", "--value", "af_bella")[0] == 2
    assert _cli("set-rule", "--scope", "session", "--target", "s", "--field", "pitch", "--value", "1")[0] == 2
    assert _cli("set-rule", "--scope", "session", "--target", "s", "--field", "volume")[0] == 2
    rc, out = _cli("clear-rule", "--scope", "voice", "--target", "bm_george", "--field", "rate")
    assert rc == 0 and "voice:bm_george" not in out["rules"]
    rc, out = _cli("clear-rule", "--scope", "project", "--target", sc.identify(cwd)["project"], "--field", "volume")
    assert (rc, out) == (0, {"rules": {}})


def test_voice_scope_lookup_skips_git_and_config_when_unused(monkeypatch):
    sc.set_rule("session", "s", 1.1, "rate")
    sc.set_rule("all", "*", 3, "frequency")
    monkeypatch.setattr(subprocess, "run", MagicMock(side_effect=AssertionError("git ran")))
    monkeypatch.setattr(sc, "voices_info", MagicMock(side_effect=AssertionError("config read")))
    assert sc.resolve_setting("rate", "s", "/some/cwd") == (1.1, "session")
    assert sc.resolve_setting("frequency", "s", "/some/cwd") == (3, "all")
    assert sc.override_voice("s", "/some/cwd") is None
