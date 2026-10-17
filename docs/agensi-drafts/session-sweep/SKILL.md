---
name: session-sweep
summary: Detect a Claude Code update and safely cycle stale cmux-hosted sessions (idle now; busy via interrupt-or-wait).
description: >-
  Companion to self-cycle. Runs sweep.sh interactively, classifies cmux Claude Code
  sessions as current / grace / idle-stale / busy-stale, cycles idle ones in place,
  and prompts for busy ones. Self-refresh the driver last via self-cycle.
  Requires cmux. Canonical repo: https://github.com/chendrizzy/session-sweep
pricing_type: paid
price: 5
tags: [agents-orchestration, workflow-automation]
keywords: [session-sweep, cmux, claude-update, cycle, stale-session, self-refresh, macos, fleet]
use_cases:
  - After a Claude Code update, refresh every open cmux session still on the old build
  - Preview with --dry-run which panes would cycle
  - Interrupt-or-wait for busy stale sessions without losing tracked resume state
permissions:
  - Run local sweep.sh (and prefer rtk proxy so protocol lines are not filtered away)
  - Call cmux CLI across workspaces (respawn-pane --workspace)
  - Run claude update (takes the single-instance lock)
  - Read/write skill state/ and optional LaunchAgent logs
  - AskUserQuestion for INTERACTIVE_PROMPT busy sessions
expected_output: >-
  Protocol lines: CYCLED | CYCLE_FAILED | GRACE | CYCLE (dry-run) |
  INTERACTIVE_PROMPT. Report counts honestly. Force-cycle only after user
  chooses Interrupt. Refresh own session last via self-cycle refresh.
---

# session-sweep

Check for a Claude Code update and safely cycle open **cmux-hosted** sessions
still on an older version. Idle sessions restart in place (same pane, same
conversation via cmux's tracked resume). Busy sessions get an interrupt-or-wait
choice.

**Canonical repo:** https://github.com/chendrizzy/session-sweep  
Install: `npx skills add chendrizzy/session-sweep`

The engine is `sweep.sh` in that repo (with `sweep.bats`). This listing is the
operating procedure. It does not inline the script.

Companion: **self-cycle** (`https://github.com/chendrizzy/self-cycle`) for
refreshing *this* driver session last.

## When to use

- User asks to update Claude Code sessions, refresh stale sessions, or mentions
  a new Claude Code version.
- Fleet of cmux panes; some still running an older `claude` binary.
- Preview: `sweep.sh --dry-run` before enabling any LaunchAgent.

## When not to use

- **Not on cmux** — same hard dependency as self-cycle; no tmux/iTerm fallback.
- Background-agent surfaces (cmux cannot respawn/restore those).
- Looping `--force-cycle` across many surfaces (each call re-runs `claude update`
  and serializes on the lock) — cycle one, confirm, then next.
- Refreshing your own driver mid-sweep — that must be **last**, via self-cycle
  `/self-refresh`, and skipped if mid-task / `/goal` / pending monitors.
- Trusting a missing version banner alone — verify with process age (`etime`).

## Permissions

| Need | Why |
|------|-----|
| `sweep.sh` + `bash` | Engine |
| `rtk proxy` (recommended) | Prevents output filters from hiding protocol lines |
| `cmux` with workspace-scoped respawn | Cycle panes in other workspaces |
| `claude update` | Version bump + lock |
| AskUserQuestion | Busy stale sessions |
| Writable state / logs | `state/last-sweep.log`, optional LaunchAgent log |

## Expected output

Run interactive mode:

```bash
rtk proxy ~/.claude/skills/session-sweep/sweep.sh --mode=interactive
```

If stdout looks empty, read `state/last-sweep.log` (mirrored each run).

| Line | Agent action |
|------|----------------|
| `CYCLED <surface_id>` | Done for that pane |
| `CYCLE_FAILED <surface_id>` | Tell user to look manually — do not claim updated |
| `GRACE <surface_id>` | Waiting out idle grace; no action |
| `CYCLE <surface_id>` | Dry-run preview only |
| `INTERACTIVE_PROMPT <surface_id>` | AskUserQuestion Wait vs Interrupt; on Interrupt run `--force-cycle=<id>` and check exit status |

Order: handle all other surfaces first, report to the user, then optionally
self-refresh this driver via self-cycle.

## Install / enable notes (honest)

Scheduled LaunchAgent must not exec through an external-volume symlink (launchd
`EX_CONFIG`). Prefer a plist that runs `/bin/bash <script>` and logs under
`~/Library/Logs/`. Leave enable/disable to the operator.
