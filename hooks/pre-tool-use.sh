#!/bin/bash
# Pre-Tool-Use Hook — long-running Bash heuristic + AskUserQuestion only
#
# Wave 1 rewrite (W1.A): Drop the ToolTenseManager mechanical narration
# ("Reading X" / "Read X") that was the primary noise source. Per the
# overhaul plan, this hook ONLY announces for Bash invocations whose
# command line matches a long-running heuristic regex. Everything else
# exits 0 silently so subsequent hooks see no extra activity.
#
# Wave 3 W3.A decision: KEEP these two pre-announcements on the legacy
# `speak` socket endpoint. ContentRouter explicitly silences phase=pre
# events ("pre-tool phase: silence by policy"), so routing them through
# `tool_event` would mute them. They're short, time-sensitive (must speak
# BEFORE the blocking user-question or long-running cargo/test invocation),
# and don't benefit from classification/summarization. The legacy `speak`
# endpoint stays in tts_daemon.py specifically for these two cases.
#
# Phase 4 HOOK-03 / ANNOUNCE-02 rewrite:
#   - Uses \btest\b word-boundary regex (not 'test' substring)
#   - Runner allowlist: pytest, cargo test, npm test, jest, vitest, mocha,
#     make test, go test, cargo build/run/check/clippy
#   - Deny-list: shell operators (&&, ||, ;, |, >, <, 2>&1, 2>, &) must
#     not appear as the announcement target
#   - Strips after first unquoted shell operator before pattern matching
#   - ANNOUNCE-02: if no allowlist match, exit silently (no generic fallback)
#   - More substantive announcements: runner + target file basename or dir

# PreToolUse stdout is Claude Code's hook protocol, not a passthrough channel.
# Empty stdout + exit 0 = no opinion (fail open). Do not echo $INPUT and do
# not emit Cursor's {"permission":"allow"} — both fail Claude Code schema
# checks ("invalid JSON") on every matching tool, in every agent.
# Quarantine later TTS chatter on stderr so it cannot corrupt the protocol.
set +e
INPUT=$(cat 2>/dev/null || printf '%s' '{}')
exec 1>&2

TTS_ENABLED="${CLAUDE_TTS_ENABLED:-true}"
[[ "$TTS_ENABLED" != "true" ]] && exit 0

# Parse the tool name from the payload we already buffered. Never echo $INPUT
# to stdout — stdout belongs to the permission protocol response above.
TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)

# Session id MUST come from the JSON payload — Claude Code does NOT set
# CLAUDE_SESSION_ID as an env var.
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "default"' 2>/dev/null)
# Per-session mute (panel); name matches daemon.session_control.safe_sid.
if [ -e "${AGENT_TTS_CONTROL_DIR:-$HOME/.claude/tts_control}/muted/$(printf '%s' "${SESSION_ID:-default}" | LC_ALL=C tr -c 'A-Za-z0-9_-' '_' | cut -c1-128)" ]; then
    exit 0
fi
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOCKET_PATH="${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}"
PYTHON_BIN="${CLAUDE_TTS_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi

# AskUserQuestion: announce the FIRST question text BEFORE the user answers.
# PreToolUse fires when Claude initiates the tool call; PostToolUse only
# fires after the user has already answered, by which point the audio is
# pointless. Speak with HIGH priority so it jumps any pending status chatter.
if [[ "$TOOL_NAME" == "AskUserQuestion" ]]; then
    QUESTION=$(echo "$INPUT" | jq -r '.tool_input.questions[0].question // empty' 2>/dev/null)
    if [[ -n "$QUESTION" && -S "$SOCKET_PATH" && -x "$PYTHON_BIN" ]]; then
        # ANNOUNCE-01: cap at first sentence OR ≤80 chars, whichever is shorter.
        # A 50-word question produces 15-25 s of speech — far too long for a
        # pre-answer cue. We want a brief "attention ping", not a reading.
        QUESTION=$("$PYTHON_BIN" "$HOOK_DIR/_ask_trim.py" "$QUESTION" 2>/dev/null)
        "$PYTHON_BIN" "$HOOK_DIR/_socket_send.py" speak "$SOCKET_PATH" "$SESSION_ID" 9 pre-tool-use-question "$QUESTION" 2>/dev/null &
    fi
    exit 0
fi

# Past here: Bash long-running heuristic only.
[[ "$TOOL_NAME" != "Bash" ]] && exit 0

COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
[[ -z "$COMMAND" ]] && exit 0

# HOOK-03: Runner allowlist + word-boundary + operator stripping.
# Shell operators must not appear as announcement targets.
# ANNOUNCE-02: if no allowlist runner matches, exit SILENTLY — no generic fallback.
ANNOUNCE=$("$PYTHON_BIN" "$HOOK_DIR/_parser_logic.py" "$COMMAND" 2>/dev/null)

[[ -z "$ANNOUNCE" ]] && exit 0

# Forward to daemon via the legacy `speak` socket command. Wave 3 W3.A
# kept this on the legacy path — see header comment for rationale.
# (SOCKET_PATH already declared at top.)
if [[ -S "$SOCKET_PATH" && -x "$PYTHON_BIN" ]]; then
    "$PYTHON_BIN" "$HOOK_DIR/_socket_send.py" speak "$SOCKET_PATH" "$SESSION_ID" 7 pre-tool-use "$ANNOUNCE" 2>/dev/null &
fi

exit 0
