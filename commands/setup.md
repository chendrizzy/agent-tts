---
name: tts:setup
description: First-run setup for claude-tts — bootstraps the engine, LLM backend, OS service, and config so Claude can speak.
---

You are running first-run setup for the **claude-tts** plugin.

Invoke the **tts-setup** skill and follow it end-to-end. The skill bootstraps:

1. Detect the platform and architecture (macOS/Linux, arm64/x86_64).
2. Ensure `uv` and a project virtual environment are available.
3. Pick the speech engine.
4. Ask whether voices vary per project or stay shared across all projects.
5. Ask for voice gender policy: balanced, female-only, male-only, mostly female,
   or mostly male. The stable config values are `balanced`, `female_only`,
   `male_only`, `female_preferred`, and `male_preferred`; mostly means 80/20.
6. Pick the LLM backend for content routing.
7. Calibrate the chosen backend against the bundled mini-eval.
8. Install the OS background service (launchd on macOS, systemd --user on Linux).
9. Write `~/.config/claude-tts/config.json`.
10. Verify the daemon starts and the socket binds at
   `${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}`.

Run the skill now. Do not hardcode absolute paths — always use `~/.config/claude-tts`
and the XDG socket expression above. If a step cannot complete, report which step
failed and the single most likely remediation, then stop.

Note: the full bootstrap and calibration logic lives in the tts-setup skill. If the
skill reports it is a stub, tell the user setup automation is not yet available in
this build and point them at `/tts:doctor` to check current health.

Only `edge-tts` and `kokoro` expose bundled gender metadata in this plugin. For
`say`, `espeak`, and `voicebox`, setup must keep `project_voices.enabled=false`
and explain that gender/per-project assignment is not enforceable for that
engine. Changing a generated voice pool can remap projects, so restart the daemon
after changing these preferences.

Optional post-setup integrations (not part of the automated skill flow):

- Cursor editor. To drive TTS from Cursor too, manually wire the
  `cursor-*.sh` hooks (`cursor-post-tool-use.sh`,
  `cursor-after-agent-response.sh`) into Cursor's hook config — they are *not*
  registered in `hooks/hooks.json`, so Claude Code never loads them. They map
  Cursor's `postToolUse`/`afterAgentResponse` events onto the
  Claude Code hooks with `CLAUDE_TTS_PASSTHROUGH=false` (the gate defaults to
  `true`; setting it false keeps Cursor's stdout clean). On macOS the
  after-response hook uses GNU `timeout` — `brew install coreutils`. Knobs:
  `CLAUDE_TTS_ENABLED`, `CLAUDE_TTS_PYTHON`, `CLAUDE_TTS_PASSTHROUGH`. See the
  Cursor section of the README for the full steps.

- 🔊 statusline segment. Everything spoken is logged to a bounded per-session
  JSONL (`~/.claude/logs/tts/spoken/<session>.jsonl`), readable with `/tts:log`;
  sub-agents and background-agents each get their own session file. The
  `statusline` config block (`subagent_aware`, `active_window_s`,
  `include_subagent_in_main`) is VIEW-ONLY — it tunes what that view shows, not
  what the daemon logs. The repo ships the config block and the spoken log; the
  wrapper that renders the live 🔊 segment in your statusline is a
  personal/global file and is *not* shipped here.
