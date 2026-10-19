"""Voice settings: config_io.update_config, config-get/config-set, and the per-message scoped rate."""
import asyncio
import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon import config_io, paths, session_control as sc  # noqa: E402
from daemon.engines.edge_tts_engine import EdgeTTSEngine  # noqa: E402
from daemon.engines.system_tts_engine import SystemTTSEngine  # noqa: E402
from daemon.pipeline.generate_stage import GenerateStage  # noqa: E402
from daemon.pipeline.orchestrator import TTSPipelineOrchestrator  # noqa: E402


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TTS_CONTROL_DIR", str(tmp_path / "ctl"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # fallback candidates resolve under tmp
    monkeypatch.setenv("CLAUDE_TTS_CONFIG", str(tmp_path / "cfg" / "config.json"))
    sc._IDENT_CACHE.clear()
    sc._RULES_CACHE.update(sig=None, rules={})
    return tmp_path


def _seed(cfg):
    p = paths.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2))
    return p


BASE = {"voice": {"engine": "kokoro", "name": "af_heart", "rate": 1.0, "volume": 1.0},
        "project_voices": {"enabled": False, "pool": ["af_bella"]}}


@pytest.mark.parametrize("key,value", [
    ("voice.name", "bf_emma"), ("voice.rate", 1.5), ("voice.volume", 0), ("voice.volume", 2),
    ("project_voices.enabled", True), ("project_voices.pool", ["af_bella", "am_michael"]),
])
def test_update_config_sets_one_key(key, value):
    p = _seed(BASE)
    out = config_io.update_config(key, value)
    group, field = key.split(".")
    assert out[group][field] == value
    assert json.loads(p.read_text()) == out
    other = {**BASE, group: {k: v for k, v in BASE[group].items() if k != field}}
    assert {**out, group: {k: v for k, v in out[group].items() if k != field}} == other


@pytest.mark.parametrize("key,value", [
    ("voice.name", ""), ("voice.name", "nope"), ("voice.name", 3),
    ("voice.rate", 0.4), ("voice.rate", 2.1), ("voice.rate", True), ("voice.rate", "1"),
    ("voice.volume", -0.1), ("voice.volume", 4.5),
    ("project_voices.enabled", "true"),
    ("project_voices.pool", []), ("project_voices.pool", [" "]), ("project_voices.pool", ["a"] * 65),
    ("project_voices.pool", "af_bella"), ("voice.engine", "say"),
])
def test_update_config_validation_leaves_file_identical(key, value):
    p = _seed(BASE)
    before = p.read_bytes()
    with pytest.raises(ValueError):
        config_io.update_config(key, value)
    assert p.read_bytes() == before


def test_update_config_refuses_broken_json():
    p = paths.config_path()
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    with pytest.raises(ValueError):
        config_io.update_config("voice.rate", 1.2)
    assert p.read_text() == "{not json"


def test_update_config_unlisted_voice_ok_when_engine_has_no_catalog():
    _seed({"voice": {"engine": "say", "name": "x"}})
    assert config_io.update_config("voice.name", "Samantha")["voice"]["name"] == "Samantha"


def test_update_config_picks_the_file_the_daemon_reads(tmp_path):
    fallback = tmp_path / "home" / ".claude" / "tts" / "config" / "tts_user_config.json"
    fallback.parent.mkdir(parents=True)
    fallback.write_text(json.dumps(BASE))
    config_io.update_config("voice.rate", 1.3)
    assert json.loads(fallback.read_text())["voice"]["rate"] == 1.3
    assert not paths.config_path().exists()


def test_update_config_creates_new_file_0600_and_no_tmp_left(tmp_path):
    config_io.update_config("voice.rate", 1.3)
    p = paths.config_path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    assert [f.name for f in p.parent.iterdir()] == ["config.json"]


def test_update_config_never_chmods_existing_parent():
    p = _seed(BASE)
    p.parent.chmod(0o755)
    config_io.update_config("voice.rate", 1.3)
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o755


def test_update_config_takes_the_config_lock(monkeypatch):
    _seed(BASE)
    names = []
    real = sc.locked
    monkeypatch.setattr(sc, "locked", lambda n: (names.append(n), real(n))[1])
    config_io.update_config("voice.rate", 1.3)
    assert names == ["config.lock"]


def test_write_config_atomic_and_leaves_parent_mode(tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    d.chmod(0o755)
    config_io.write_config({"a": 1}, d / "c.json")
    assert stat.S_IMODE(d.stat().st_mode) == 0o755
    assert stat.S_IMODE((d / "c.json").stat().st_mode) == 0o600
    assert [f.name for f in d.iterdir()] == ["c.json"]


def test_write_config_failure_keeps_old_file_and_no_tmp(tmp_path):
    d = tmp_path / "c.json"
    d.write_text("old")
    with pytest.raises(TypeError):
        config_io.write_config({"a": object()}, d)
    assert d.read_text() == "old" and not [f for f in tmp_path.iterdir() if ".tmp." in f.name]


# --- Phase 2 review fixes ------------------------------------------------------
def test_voice_name_validated_when_engine_unset():
    _seed({"voice": {"name": "en-US-AvaNeural"}})   # config-get reports edge-tts
    with pytest.raises(ValueError, match="unknown voice"):
        config_io.update_config("voice.name", "nope")
    assert config_io.update_config("voice.name", "en-US-GuyNeural")["voice"]["name"] == "en-US-GuyNeural"


def test_write_config_takes_the_config_lock(tmp_path, monkeypatch):
    names = []
    real = sc.locked
    monkeypatch.setattr(sc, "locked", lambda n: (names.append(n), real(n))[1])
    config_io.write_config({"a": 1}, tmp_path / "c.json")
    assert names == ["config.lock"]


def test_symlinked_config_stays_a_symlink(tmp_path):
    real = tmp_path / "dotfiles" / "real.json"
    real.parent.mkdir()
    real.write_text(json.dumps(BASE))
    link = paths.config_path()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)
    config_io.update_config("voice.rate", 1.3)
    assert link.is_symlink() and json.loads(real.read_text())["voice"]["rate"] == 1.3


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores dir modes")
def test_readonly_dir_writable_file_falls_back_to_in_place_write(tmp_path):
    p = _seed(BASE)
    p.parent.chmod(0o555)
    try:
        assert config_io.update_config("voice.rate", 1.3)["voice"]["rate"] == 1.3
        assert json.loads(p.read_text())["voice"]["rate"] == 1.3
    finally:
        p.parent.chmod(0o755)


def test_pool_entries_validated_against_engine_catalog():
    p = _seed({"voice": {"engine": "edge-tts", "name": "en-US-AvaNeural"}})
    before = p.read_bytes()
    with pytest.raises(ValueError, match="unknown voice"):
        config_io.update_config("project_voices.pool", ["nope"])
    assert p.read_bytes() == before
    assert config_io.update_config("project_voices.pool", ["en-US-GuyNeural"])["project_voices"]["pool"] == ["en-US-GuyNeural"]


def test_existing_non_dict_group_is_refused_not_replaced():
    p = _seed({"voice": "en-US-AvaNeural"})
    before = p.read_bytes()
    with pytest.raises(ValueError, match="voice"):
        config_io.update_config("voice.rate", 1.2)
    assert p.read_bytes() == before


def test_write_never_lands_in_the_in_repo_default(tmp_path, monkeypatch):
    repo = tmp_path / "pkg"
    repo.mkdir()
    default = repo / "tts_user_config.json"
    default.write_text(json.dumps(BASE))
    before = default.read_bytes()
    cands = lambda: [paths.config_path(), default]
    monkeypatch.setattr(config_io, "config_candidates", cands)
    monkeypatch.setattr(config_io, "_SOURCE_TREE", repo)
    out = config_io.update_config("voice.rate", 1.3)
    assert default.read_bytes() == before
    assert config_io.active_config_path() == paths.config_path()
    assert json.loads(paths.config_path().read_text()) == out == {**BASE, "voice": {**BASE["voice"], "rate": 1.3}}


def _cli(capsys, *argv):
    rc = sc.main(list(argv))
    return rc, json.loads(capsys.readouterr().out)


def test_cli_config_get(capsys):
    p = _seed(BASE)
    rc, out = _cli(capsys, "config-get")
    assert rc == 0
    assert out["path"] == str(p) and out["config_error"] is None
    assert out["voice"] == BASE["voice"] and out["project_voices"] == BASE["project_voices"]
    assert "af_heart" in out["voices"] and "restart_needed" not in out


def test_cli_config_get_without_file_uses_daemon_defaults(capsys):
    rc, out = _cli(capsys, "config-get")
    assert rc == 0 and out["voice"]["engine"] == "edge-tts" and out["voice"]["rate"] == 1.0


def test_cli_config_get_reports_broken_file(capsys):
    p = paths.config_path()
    p.parent.mkdir(parents=True)
    p.write_text("[")
    rc, out = _cli(capsys, "config-get")
    assert rc == 0 and out["config_error"]


@pytest.mark.parametrize("key,raw,expect", [
    ("voice.rate", "1.5", 1.5), ("voice.name", "bf_emma", "bf_emma"),
    ("project_voices.enabled", "true", True), ("project_voices.pool", '["af_bella"]', ["af_bella"]),
])
def test_cli_config_set(capsys, key, raw, expect):
    _seed(BASE)
    rc, out = _cli(capsys, "config-set", "--key", key, "--value", raw)
    g, f = key.split(".")
    assert rc == 0 and out["restart_needed"] is True
    assert (out["voice"] if g == "voice" else out["project_voices"])[f] == expect


@pytest.mark.parametrize("key,raw", [
    ("voice.rate", "9"), ("voice.rate", "fast"), ("project_voices.enabled", "yes"),
    ("project_voices.pool", "[]"), ("voice.engine", "say"),
])
def test_cli_config_set_errors_exit_2_file_untouched(capsys, key, raw):
    p = _seed(BASE)
    before = p.read_bytes()
    rc, out = _cli(capsys, "config-set", "--key", key, "--value", raw)
    assert rc == 2 and out["error"] and p.read_bytes() == before


# --- scoped rate -----------------------------------------------------------
def _orch(tmp_path, speed):
    return TTSPipelineOrchestrator(cache_dir=str(tmp_path / "cache"), speed=speed)


@pytest.mark.parametrize("master,rule,expect", [
    (1.2, None, 1.2),    # no rule: global
    (2.5, None, 2.5),    # no rule: a hand-edited global rate is not clamped
    (1.2, 1.5, 1.8),     # global x scoped
    (1.5, 2.0, 2.0),     # clamped high
    (0.6, 0.5, 0.5),     # clamped low
])
def test_message_speed(tmp_path, master, rule, expect):
    if rule is not None:
        sc.set_rule("session", "S", rule, "rate")
    assert asyncio.run(_orch(tmp_path, master)._message_speed("S")) == pytest.approx(expect)


def test_generate_passes_per_message_speed_to_engine_and_keys_cache_on_it(tmp_path):
    seen = []

    class Eng:
        async def synthesize(self, text, out, voice, speed):
            seen.append(speed)
            open(out, "wb").write(b"x")
            return True

    gs = GenerateStage(cache_dir=str(tmp_path), min_free_bytes=1, speed=1.0)
    gs._engine = Eng()
    proc = SimpleNamespace(session_id="s", request_id="r", chunks=["hi"])

    async def run(speed):
        return [s async for s in gs.generate(proc, "v", speed)]

    asyncio.run(run(None))      # None = global default
    asyncio.run(run(1.5))       # different rate must not hit the cached 1.0 audio
    assert seen == [1.0, 1.5]


# --- engine mapping --------------------------------------------------------
class _Comm:
    seen = None

    def __init__(self, text, voice, rate="+0%"):
        _Comm.seen = rate

    async def save(self, path):
        open(path, "wb").write(b"x")


@pytest.mark.parametrize("speed,rate", [(1.0, "+0%"), (1.2, "+20%"), (0.5, "-50%"), (2.0, "+100%")])
def test_edge_rate_string(tmp_path, speed, rate):
    e = EdgeTTSEngine()
    e._mod = SimpleNamespace(Communicate=_Comm)
    assert asyncio.run(e.synthesize("hi", str(tmp_path / "a.mp3"), "v", speed))
    assert _Comm.seen == rate


@pytest.mark.parametrize("system,binary,flag", [("Darwin", "say", "-r"), ("Linux", "espeak", "-s")])
def test_system_rate_flag(monkeypatch, system, binary, flag):
    import platform, shutil
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/espeak" if b == "espeak" else None)
    cmd = SystemTTSEngine()._build_cmd("hi", "/tmp/x.wav", 1.2)
    assert cmd[cmd.index(flag) + 1] == "210"            # 175 wpm x 1.2
    assert flag not in SystemTTSEngine()._build_cmd("hi", "/tmp/x.wav", 1.0)


def test_kokoro_request_carries_speed(tmp_path):
    from daemon.pipeline.kokoro_engine import KokoroEngine
    k = KokoroEngine.__new__(KokoroEngine)
    written = []

    class Stdin:
        def write(self, b): written.append(json.loads(b))
        async def drain(self): raise RuntimeError("stop")

    k._lock = asyncio.Lock()
    k._proc = SimpleNamespace(stdin=Stdin())
    k._req_counter, k.default_voice, k.lang_code = 0, "v", "a"
    with pytest.raises(RuntimeError):
        asyncio.run(k._do_synthesize("hi", "/tmp/o.wav", "v", 1.4))
    assert written[0]["speed"] == 1.4
