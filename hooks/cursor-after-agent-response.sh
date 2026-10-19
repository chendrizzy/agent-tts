#!/bin/bash
# Cursor afterAgentResponse -> daemon stop_event (assistant final answer)

set -u

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCKET="/tmp/tts_daemon.sock"
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

LOG_DIR="$HOME/.claude/logs/hooks"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/cursor_after_agent_$(date +%Y%m%d).log"
log() { echo "[$(date +'%H:%M:%S')] Cursor-AfterAgent: $1" >> "$LOG_FILE"; }

# Per-session mute (panel); sid precedence matches cursor_normalize.py (session_id first); name matches daemon.session_control.safe_sid.
SID=$(jq -r '[.session_id, .conversation_id] | map(select(. != null and . != "")) | (.[0] // "default")' <<< "$INPUT" 2>/dev/null)
if [ -e "${AGENT_TTS_CONTROL_DIR:-$HOME/.claude/tts_control}/muted/$(printf '%s' "${SID:-default}" | LC_ALL=C tr -c 'A-Za-z0-9_-' '_' | cut -c1-128)" ]; then
    log "stop_event skipped: session muted"
    exit 0
fi

if [ ! -S "$SOCKET" ] || ! /usr/bin/pgrep -f "python.*tts_daemon.py" > /dev/null 2>&1; then
    source "$PLUGIN_ROOT/hooks/ensure-daemon-ready.sh" 2>>"$LOG_FILE" || true
    if [ ! -S "$SOCKET" ]; then
        log "WARN: daemon unavailable — skipping stop_event"
        exit 0
    fi
fi

NORMALIZED=$("$PYTHON_BIN" "$PLUGIN_ROOT/hooks/cursor_normalize.py" agent_response <<< "$INPUT" 2>/dev/null) || exit 0
[ -n "$NORMALIZED" ] || exit 0

(
    printf '%s' "$NORMALIZED" | "$PYTHON_BIN" "$PLUGIN_ROOT/hooks/_cursor_stop_event.py" "$SOCKET" 2>>"$LOG_FILE"
) &

log "stop_event dispatch STARTED (async; the child logs the outcome)"
exit 0
