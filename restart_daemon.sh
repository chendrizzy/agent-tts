#!/bin/sh
# Restart the TTS daemon through its OS service (launchd / systemd --user / unmanaged).
# A manual restart is an explicit Boot: it clears the panel's Kill "stay down" flag.
# Prints the daemon-status JSON; exits 1 with a reason if it did not come back within 15 s.
cd "$(dirname "$0")" || exit 1
exec "${CLAUDE_TTS_PYTHON:-python3}" daemon/session_control.py daemon-restart
