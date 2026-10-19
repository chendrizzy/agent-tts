import json
from pathlib import Path

HOOKS = Path(__file__).parent.parent / "hooks"


def test_no_dead_speak_hooks_or_session_end():
    # PreToolUse sent a 'speak' the daemon rejects; SessionEnd re-spoke the last reply.
    cfg = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
    assert "PreToolUse" not in cfg and "SessionEnd" not in cfg
    assert not (HOOKS / "pre-tool-use.sh").exists()
