#!/bin/bash
# Defensive Daemon Readiness Check
# LAYER 3: Defensive hook-level checks
# Ensures daemon is responsive before any hook attempts to use it

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCKET="${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}"
PYTHON_BIN="${CLAUDE_TTS_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi

# OBSERVE-02 thresholds — easy to tune. Values mirror the spec:
#   * AUDIO_STALE_THRESHOLD_S   — warn if no audio in this long...
#   * SESSION_ACTIVE_WINDOW_S   — ...AND a tool_event was seen within this long
# Combined, this catches "session is firing tool_events but daemon has gone
# silent" without false-positives during legitimate idle periods.
AUDIO_STALE_THRESHOLD_S=300
SESSION_ACTIVE_WINDOW_S=60

# Quick daemon readiness check
ensure_daemon_ready() {
    # Fast path: socket exists and daemon process is alive. Avoid spawning
    # Python for readiness; pyenv shim startup can be slower than hook budgets.
    if [ -S "$SOCKET" ]; then
        if /usr/bin/pgrep -f "tts_daemon.py" > /dev/null 2>&1; then
            # Daemon socket is up — opportunistically check audio staleness
            # so silent-session-death is surfaced.  Best-effort: never block
            # the hook on this; never auto-restart (just warn to stderr).
            check_audio_staleness
            return 0
        else
            rm -f "$SOCKET" 2>/dev/null
        fi
    fi

    # Daemon not ready. Do not autostart from every hook by default; when the
    # daemon fails to stay alive, hook autostart becomes a process-spawn storm.
    # Set CLAUDE_TTS_AUTOSTART=true only for an explicit manual recovery run.
    if [ "${CLAUDE_TTS_AUTOSTART:-false}" = "true" ]; then
        "$PLUGIN_ROOT/hooks/session-start.sh" >&2
        return $?
    fi

    echo "WARN(tts): daemon unavailable; autostart disabled" >&2
    return 1
}

# OBSERVE-02 — audio staleness warning.
# Calls the daemon's `health` socket command and warns on stderr (never
# auto-restarts) when:
#   * a session is currently active (tool_event in the last
#     SESSION_ACTIVE_WINDOW_S seconds), AND
#   * no successful audio playback in the last AUDIO_STALE_THRESHOLD_S
#     seconds (seconds_since_last_audio > 300).
#
# This catches the failure mode where Claude Code keeps firing hooks
# but the daemon's playback path has stopped producing audio — without
# this check, the operator only notices when they realise the room is
# silent.  No-op if jq is unavailable or if the daemon hasn't yet
# played any audio (last_audio_played_at == null).
check_audio_staleness() {
    command -v jq >/dev/null 2>&1 || return 0
    local response
    response=$("$PYTHON_BIN" -c "
import os as _os, signal as _signal
# Wall-clock ceiling, self-enforced. GNU coreutils 'timeout' is absent on stock
# macOS, where wrapping this invocation in it meant the hook delivered nothing
# at all. Armed before any work so it bounds file reads too, not just the
# socket. Exit 75 (EX_TEMPFAIL) marks a ceiling hit.
_signal.signal(_signal.SIGALRM, lambda *_a: _os._exit(75))
_signal.setitimer(_signal.ITIMER_REAL, 1.0)
import socket, json, sys
try:
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(0.5)
    s.connect('$SOCKET')
    s.sendall(b'{\"command\":\"health\"}\n')
    data = s.recv(8192)
    s.close()
    print(data.decode('utf-8'))
except Exception:
    sys.exit(1)
" 2>/dev/null) || return 0
    [ -n "$response" ] || return 0
    local sec_since_audio
    local sec_since_tool
    sec_since_audio=$(echo "$response" | jq -r '.seconds_since_last_audio // empty' 2>/dev/null)
    sec_since_tool=$(echo "$response" | jq -r '.seconds_since_last_tool_event // empty' 2>/dev/null)
    # Both fields must be present + numeric for the check to be meaningful.
    [ -n "$sec_since_audio" ] || return 0
    [ -n "$sec_since_tool" ] || return 0
    # Compare as integers (truncate decimals).  Active session AND stale audio.
    local audio_int=${sec_since_audio%%.*}
    local tool_int=${sec_since_tool%%.*}
    if [ "$tool_int" -le "$SESSION_ACTIVE_WINDOW_S" ] 2>/dev/null && \
       [ "$audio_int" -gt "$AUDIO_STALE_THRESHOLD_S" ] 2>/dev/null; then
        echo "WARN(tts): audio silent for ${audio_int}s during active session " \
             "(tool_event ${tool_int}s ago); investigate playback pipeline." >&2
    fi
}

# Only run if sourced (not executed directly)
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    ensure_daemon_ready
fi
