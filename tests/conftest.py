"""Suite-wide isolation: no test may read or write the real user config or control dir."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_user_state(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("iso")
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg"))
    monkeypatch.setenv("CLAUDE_TTS_CONFIG", str(root / "cfg" / "config.json"))
    monkeypatch.setenv("AGENT_TTS_CONTROL_DIR", str(root / "ctl"))
