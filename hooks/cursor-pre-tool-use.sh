#!/bin/bash
# Cursor preToolUse -> Claude Code-shaped pre-tool announcements (via pre-tool-use.sh)

set -u

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${CLAUDE_TTS_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi

INPUT=$(cat)

TTS_ENABLED="${CLAUDE_TTS_ENABLED:-true}"
if [[ "$TTS_ENABLED" != "true" ]]; then
    exit 0
fi

if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    exit 0
fi

# Per-session mute (panel); sid precedence matches cursor_normalize.py (session_id first); name matches daemon.session_control.safe_sid.
SID=$(jq -r '[.session_id, .conversation_id] | map(select(. != null and . != "")) | (.[0] // "default")' <<< "$INPUT" 2>/dev/null)
if [ -e "${AGENT_TTS_CONTROL_DIR:-$HOME/.claude/tts_control}/muted/$(printf '%s' "${SID:-default}" | LC_ALL=C tr -c 'A-Za-z0-9_-' '_' | cut -c1-128)" ]; then
    exit 0
fi

NORMALIZED=$("$PYTHON_BIN" "$PLUGIN_ROOT/hooks/cursor_normalize.py" pre <<< "$INPUT" 2>/dev/null) || exit 0
[ -n "$NORMALIZED" ] || exit 0

CLAUDE_TTS_PASSTHROUGH=false bash "$PLUGIN_ROOT/hooks/pre-tool-use.sh" <<< "$NORMALIZED" >/dev/null 2>&1 &
exit 0
