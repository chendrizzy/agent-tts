#!/bin/bash
# Speech Output Hook for Claude Code TTS — Stop-hook handler
#
# Wave 3 W3.A cutover: legacy `client.speak()` Python block REMOVED. ALL
# events now route through the new `stop_event` socket endpoint, which
# feeds ContentRouter → QueueManager → pipeline. The Wave 2.5 shadow
# dual-write is now the PRIMARY path (no `shadow: true` flag).
#
# Wired by ~/.claude/settings.json to:
#   - Stop  (matcher: "*")     → JSON Stop payload with transcript_path
#   - Notification (matcher: "")  → may be raw text (legacy leftover, harmless)
#
# Stop_hook_active is honored by the daemon (skipped to prevent infinite
# loops). Transcript-path JSONL extraction handled inline below.

# Read the output from stdin
OUTPUT=$(cat)

# Always echo the output first (passthrough behavior)
printf '%s\n' "$OUTPUT"   # printf, not echo: sh's echo eats backslashes and corrupts the JSON passthrough

# Skip if TTS is disabled
TTS_ENABLED="${CLAUDE_TTS_ENABLED:-true}"
if [[ "$TTS_ENABLED" != "true" ]]; then
    exit 0
fi

# Logging
LOG_DIR="$HOME/.claude/logs/hooks"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/speech_output_$(date +%Y%m%d).log"
log() { echo "[$(date +'%H:%M:%S')] Speech-Output: $1" >> "$LOG_FILE"; }

# Per-session mute (panel); name matches daemon.session_control.safe_sid. Raw-text
# (Notification) input has no JSON session id -> env/default, like the python below.
SID=$(jq -r '.session_id // empty' <<< "$OUTPUT" 2>/dev/null)
SID="${SID:-${CLAUDE_SESSION_ID:-default}}"
if [ -e "${AGENT_TTS_CONTROL_DIR:-$HOME/.claude/tts_control}/muted/$(printf '%s' "${SID:-default}" | LC_ALL=C tr -c 'A-Za-z0-9_-' '_' | cut -c1-128)" ]; then
    log "stop_event skipped: session muted"
    exit 0
fi

# Notification prefs (notifications.json). Kind rule mirrors
# daemon.session_control.notification_kind; tests/test_notifications.py pins the parity.
NOTIF_KIND=""
if [ "$(jq -r '.hook_event_name // empty' <<< "$OUTPUT" 2>/dev/null)" = "Notification" ]; then
    NOTIF_KIND=$(jq -r '(.notification_type // "") as $t | ((.message | strings | ascii_downcase) // "") as $m
        | if $t == "permission_prompt" or $t == "idle_prompt" then $t
          elif ($m | contains("permission")) then "permission_prompt"
          elif ($m | contains("waiting for your input") or contains("waiting for input")) then "idle_prompt"
          else "other" end' <<< "$OUTPUT" 2>/dev/null)
    NOTIF_KIND="${NOTIF_KIND:-other}"
    if [ "$(jq -r --arg k "$NOTIF_KIND" 'if .speak[$k] == false then "off" else "on" end' \
            "${AGENT_TTS_CONTROL_DIR:-$HOME/.claude/tts_control}/notifications.json" 2>/dev/null)" = "off" ]; then
        log "notification skipped: $NOTIF_KIND off"
        exit 0
    fi
fi
PYTHON_BIN="${CLAUDE_TTS_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi

# Require python3 — degrade gracefully on missing deps.
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    log "WARN: python3 unavailable — skipping daemon submission"
    exit 0
fi

# Defensive readiness check. Degrade gracefully — never `exit 0` on failure
# in a way that hides the daemon being down; just skip the submission.
SOCKET="${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}"
if [ ! -S "$SOCKET" ]; then
    log "WARN: daemon socket missing — skipping stop_event submission"
    exit 0
fi

# Build + dispatch stop_event in background. Reads transcript_path from JSON
# Stop payload, walks the JSONL backwards to find the latest assistant text,
# then forwards as `stop_event`. If input is raw text (Notification path),
# treat it as content directly.
(
    printf '%s' "$OUTPUT" | NOTIF_KIND="$NOTIF_KIND" "$PYTHON_BIN" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_stop_event.py" "$SOCKET" 2>>"$LOG_FILE"
) &

log "stop_event dispatch STARTED (async; the child logs the outcome)"
exit 0
