"""Gate tests for config assembly + write (daemon/config_io.py)."""
import sys
import json
import stat
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daemon.config_io import render_config, write_config  # noqa: E402

PII_FORBIDDEN = ("/Volumes/", "com.justinchen", "@gmail", "profile_id",
                 "/opt/anaconda", "anaconda3", "mlx_python")


def test_render_config_ollama_shape():
    cfg = render_config(engine="edge-tts", voice_name="en-US-AvaNeural",
                        backend="ollama", model="qwen2.5-coder:1.5b")
    assert cfg["voice"]["engine"] == "edge-tts"
    assert cfg["voice"]["name"] == "en-US-AvaNeural"
    assert cfg["llm_provider"]["type"] == "ollama"
    assert cfg["summarizer"]["model"] == "qwen2.5-coder:1.5b"


def test_render_config_openai_shape():
    cfg = render_config(engine="edge-tts", voice_name="v", backend="openai",
                        model="gpt-4o-mini", base_url="http://localhost:1234/v1",
                        api_key="sk-test")
    assert cfg["llm_provider"] == {
        "type": "openai", "base_url": "http://localhost:1234/v1",
        "model": "gpt-4o-mini", "api_key": "sk-test",
    }


def test_render_config_null_is_deterministic_mode():
    cfg = render_config(engine="say", voice_name="Alex", backend="null")
    assert cfg["llm_provider"] == {"type": "null"}
    assert "summarizer" not in cfg  # no model to configure


def test_render_config_injects_no_pii():
    cfg = render_config(engine="edge-tts", voice_name="v", backend="ollama", model="m")
    text = json.dumps(cfg)
    for needle in PII_FORBIDDEN:
        assert needle not in text


def test_write_config_round_trips_and_is_private(tmp_path):
    cfg = render_config(engine="edge-tts", voice_name="v", backend="null")
    dest = tmp_path / "sub" / "config.json"
    write_config(cfg, dest)
    assert json.loads(dest.read_text()) == cfg
    # Config may carry an API key -> file is owner-only.
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_render_config_defaults_to_legacy_shared_voice_behavior():
    cfg = render_config(engine="edge-tts", voice_name="en-US-AvaNeural",
                        backend="null")
    assert cfg["project_voices"]["enabled"] is False
    assert cfg["project_voices"]["gender_preference"] == "balanced"
    assert cfg["voice"]["name"] == "en-US-AvaNeural"


def test_render_config_project_voice_scope_enables_materialized_balanced_pool():
    cfg = render_config(engine="edge-tts", voice_name="en-US-AvaNeural",
                        backend="null", project_voice_scope="per_project",
                        voice_gender_preference="balanced")
    project_voices = cfg["project_voices"]
    assert project_voices["enabled"] is True
    assert project_voices["gender_preference"] == "balanced"
    assert project_voices["preferred_ratio"] == 0.5
    assert len(project_voices["pool"]) == 8
    assert project_voices["pool"][0] == "en-US-AvaNeural"
    assert project_voices["pool"][1] == "en-US-GuyNeural"


def test_render_config_locked_gender_policies_are_single_gender():
    female = render_config(engine="kokoro", voice_name="am_michael",
                           backend="null", project_voice_scope="per_project",
                           voice_gender_preference="female_only")
    assert female["voice"]["name"] == "af_heart"
    assert female["project_voices"]["enabled"] is True
    assert female["project_voices"]["preferred_ratio"] == 1.0
    assert female["project_voices"]["pool"] == ["af_heart", "af_bella", "bf_emma"]

    male = render_config(engine="edge-tts", voice_name="en-US-AvaNeural",
                         backend="null", project_voice_scope="per_project",
                         voice_gender_preference="male_only")
    assert male["voice"]["name"] == "en-US-GuyNeural"
    assert male["project_voices"]["preferred_ratio"] == 1.0
    assert all(name in {"en-US-GuyNeural", "en-US-BrianMultilingualNeural",
                        "en-US-ChristopherNeural", "en-US-AndrewMultilingualNeural"}
               for name in male["project_voices"]["pool"])


def test_render_config_preferred_gender_policies_materialize_80_20_weighting():
    female = render_config(engine="edge-tts", voice_name="en-US-GuyNeural",
                           backend="null", project_voice_scope="per_project",
                           voice_gender_preference="female_preferred")
    pool = female["project_voices"]["pool"]
    female_slots = [name for name in pool if name in {"en-US-AvaNeural",
                                                      "en-US-EmmaMultilingualNeural",
                                                      "en-US-JennyNeural",
                                                      "en-US-AriaNeural"}]
    assert female["voice"]["name"] == "en-US-AvaNeural"
    assert female["project_voices"]["preferred_ratio"] == 0.8
    assert len(pool) == 20
    assert len(female_slots) == 16

    male = render_config(engine="kokoro", voice_name="af_heart",
                         backend="null", project_voice_scope="per_project",
                         voice_gender_preference="male_preferred")
    pool = male["project_voices"]["pool"]
    male_slots = [name for name in pool if name in {"am_michael", "bm_george"}]
    assert male["voice"]["name"] == "am_michael"
    assert male["project_voices"]["preferred_ratio"] == 0.8
    assert len(pool) == 15
    assert len(male_slots) == 12


def test_render_config_balanced_pool_is_equal_when_catalog_sizes_differ():
    cfg = render_config(engine="kokoro", voice_name="af_heart",
                        backend="null", project_voice_scope="per_project",
                        voice_gender_preference="balanced")
    pool = cfg["project_voices"]["pool"]
    female = {"af_heart", "af_bella", "bf_emma"}
    male = {"am_michael", "bm_george"}
    assert sum(name in female for name in pool) == 3
    assert sum(name in male for name in pool) == 3


def test_render_config_shared_mode_keeps_gender_compatible_default_voice():
    cfg = render_config(engine="edge-tts", voice_name="en-US-AvaNeural",
                        backend="null", project_voice_scope="shared",
                        voice_gender_preference="male_preferred")
    assert cfg["project_voices"]["enabled"] is False
    assert cfg["voice"]["name"] == "en-US-GuyNeural"
    assert cfg["project_voices"]["pool"].count("en-US-GuyNeural") >= 1


def test_render_config_unsupported_engine_does_not_claim_policy_enforcement():
    cfg = render_config(engine="say", voice_name="Alex", backend="null",
                        project_voice_scope="per_project",
                        voice_gender_preference="female_only")
    assert cfg["voice"]["name"] == "Alex"
    assert cfg["project_voices"]["enabled"] is False
    assert cfg["project_voices"]["pool"] == ["Alex"]
    assert cfg["project_voices"]["gender_enforced"] is False
