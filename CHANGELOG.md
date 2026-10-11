# Changelog

All notable changes to claude-tts are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.11] — 2026-10-11

### Fixed
- **The plugin was silent on stock macOS. Every release until now.** Each hook
  wrapped its inline Python in GNU coreutils `timeout`, which macOS does not
  ship. On any Mac without a third-party coreutils install the shell printed
  `timeout: command not found`, the Python never ran, nothing reached the
  daemon — and the hook logged `stop_event dispatched` and exited 0. It worked
  only on machines with Homebrew coreutils. Reproduced directly: with
  `PATH=/usr/bin:/bin` the daemon received 0 messages; with Homebrew on `PATH`,
  1. Six call sites across five hooks were affected, covering every delivery
  path — pre-tool announcements, tool events, turn summaries and the Cursor
  path. macOS is the primary platform, since the Kokoro engine is
  Apple-Silicon-only.

  The ceiling now lives inside the Python child (`signal.setitimer`), armed
  before any other work, so it bounds the transcript read and scan as well as
  the socket call — which the in-script socket timeout never did. Probing for
  `timeout`/`gtimeout` and running bare when absent was rejected: it keeps the
  ceiling where it is not needed and drops it on the platform that needs it.
- **The hook logs no longer claim an outcome they cannot know.** Delivery runs
  in a backgrounded subshell and cannot be awaited without stalling the agent,
  so the shell now logs that a dispatch *started*, and the child — the only
  party that learns the result — logs `DELIVERED ack=…` or `FAILED: …`. The
  old unconditional "dispatched" line is what let this bug hide for three
  releases.

### Added
- `tests/test_hook_portability.py`, in the binding `make verify` gate. It runs
  the real hooks against a real socket with `PATH=/usr/bin:/bin` and asserts
  what the **daemon received** — never the hook's exit code, which was 0
  throughout the outage — plus a source guard that fails on any command absent
  from a target platform and not shielded by its own `command -v` probe
  (`launchctl` is legitimately probed that way and still passes). Verified
  binding by reintroducing the bug: 3 of the 4 tests fail.

## [0.1.10] — 2026-10-11

### Fixed
- The Stop-hook integration test is hermetic and self-diagnosing. It built its
  child environment as `dict(os.environ, ...)`, inheriting every `CLAUDE_TTS_*`
  variable from the developer's shell and from anything that ran earlier in the
  same pytest session — so it passed locally and went red on CI (v0.1.9's
  `tests` workflow) reporting only `assert 0 == 2`. The hook has four silent
  early-exit branches and writes its reason to a log file; the test now passes
  an explicit minimal environment and, on any failure, reports the hook's
  return code, stdout, stderr and that log. Test-only — no change to shipped
  behaviour from 0.1.9.

## [0.1.9] — 2026-10-11

### Fixed
- **Subagent tool telemetry is now silent by policy.** When the main agent
  delegates to Agent/Workflow subagents, Claude Code fires a full set of hook
  events inside each one. The router never looked at them, so a parallel
  workflow narrated every helper's `Bash` output — overwhelmingly fragments of
  `sed`/`grep` text that merely *looked* like errors. The gate reads
  `agent_id` (stamped only inside a subagent) and the `/subagents/` transcript
  path, deliberately **not** `agent_type`, which is also set for a main session
  started with `claude --agent <name>`. A subagent's actual finding still
  reaches you, through the parent's turn-summary prose.
- **Errors are no longer spoken twice.** `~/.claude/settings.json` and the
  installed plugin's `hooks.json` both wire `PostToolUse`, so every event is
  delivered twice ~0.06s apart. Every speak path deduped except the ERROR
  branch, which had none. Added a per-session, time-windowed error dedupe
  (`ERROR_DEDUPE_WINDOW_S`, 30s) — it catches the twin delivery and a rapid
  re-run, while an identical failure hours later or in another project still
  speaks, because live narration is not a log.
- **A traceback now speaks its exception line, not its header.**
  `_extract_error_snippet` returned the first keyword-bearing line; for a Python
  traceback that is the fixed `Traceback (most recent call last):` boilerplate.
  It now returns the exception line after the *last* header (so chained
  tracebacks end on the outermost one), handles per-line decoration from
  `docker compose`/`kubectl`/`ExceptionGroup`, and names the innermost frame
  when the trace was truncated by a pipe.
- **A bare error keyword on stdout is no longer treated as a failure.** Claude
  Code exposes no exit code, so stdout was scanned for error *keywords* —
  which mislabeled `"error": null`, `['error', ...]`, `755 error_handler.py`,
  `grep` hits and diffs as crashes. Replaced with `GENUINE_ERROR_LINE_RE`, a
  curated allowlist of the structural shapes real failures print. stderr-sourced
  errors are unchanged: a non-empty error stream is itself the signal.
- **`hooks/ensure-daemon-ready.sh` is now shipped.** `post-tool-use.sh` and
  `cursor-after-agent-response.sh` both `source` it, so every public install
  emitted a "No such file or directory" to stderr on every tool event. The
  script existed in development and was never packaged. Shipping it also
  restores the audio-staleness warning that
  `tests/test_phase5_observability.py` already asserted.
- `PreToolUse` hooks emit empty stdout instead of a passthrough or a
  `{"permission":"allow"}` verdict — both fail Claude Code's hook schema.
- Python 3.11+ traceback caret underlines (`~~~~^^`) normalize away instead of
  being spoken as punctuation.
- Harness/API boilerplate ("Credit balance is too low", "API Error: 529 …",
  rate-limit notices) is dropped rather than read aloud as a turn summary.
- The Stop hook forwards `agent_id`/`agent_type` to the daemon, so the subagent
  policy applies to turn-end events too, and writes its JSON passthrough with
  `printf` instead of `echo`, which ate backslashes.

### Changed
- The hooks PII gate enumerates `hooks/*.sh` by glob instead of a hand-kept
  list, which had grown to cover the manifests and commands while scanning none
  of the eight shipped hook scripts.

## [0.1.8] — 2026-08-17

### Added
- `output_context` (opt-in) — name the project in spoken output when it adds
  value: first mention, a project switch, an error, or a re-anchor after
  `reanchor_gap_s` of quiet. A deterministic gate decides *whether* to name it,
  so it does not prefix every line. Complements `project_voices`: voices
  distinguish repos by sound, this names them in words.
- `speech_selection` (opt-in) — deterministic, substance-preserving selection
  on the turn-summary path, replacing the LLM's salience judgement. Blocks are
  scored (blockquotes weighted up, bare headings down, pure test/build telemetry
  dropped) and the highest-substance ones kept within a char budget,
  sentence-aware, with any trailing question spoken last. No LLM call, no
  network, same input → same speech. Fixes over-condensation dropping
  blockquoted deliverables while keeping filler.

Both ship OFF; with them off the pipeline is byte-identical (the gate holds at
the same count with the feature code present but disabled).

### Fixed
- Per-project voices did not apply to events without a `cwd` — which is the
  stop/prose path, i.e. the turn-final speech you actually hear.
  `ContentRouter._project_for_event` falls back to
  `spoken_log.get_session_cwd()`, but that accessor did not exist in this repo,
  so the call raised `AttributeError` into a best-effort `except` and silently
  resolved to no project. The daemon was recording the session cwd the whole
  time; nothing could read it back. Added the accessor and a regression test —
  the swallowing `except` means only a test can catch this class of bug.

### Changed
- Releases now quote this file. `release.yml` built its notes with
  `gh release create --generate-notes`, which lists merged PRs since the last
  tag — this repo releases straight from `main`, so v0.1.7's notes described an
  unrelated dead-code PR instead of the release. The workflow now publishes the
  CHANGELOG section for the tagged version (`scripts/release_notes.py`), and
  `make verify` fails when the version in `pyproject.toml` has no section, so a
  release cannot be cut without notes.

## [0.1.7] — 2026-08-16

### Added
- Per-project voices. The daemon now assigns a deterministic voice per repo
  (`pool[sha256(repo_basename) % len(pool)]`), resolved per message rather than
  frozen at session start, so a session that spans projects speaks each one in
  its own voice. Off unless `project_voices.enabled` is set.
- `/tts:voices` — show the deterministic project → voice map, with the current
  repo highlighted, so an audible cue becomes visible. Read-only.
- `/tts:log` tags every line with its `project · voice`, so the replay shows
  which repo each utterance came from and which voice spoke it.
- `/tts:setup` now asks whether voices should vary per project or stay shared
  across all projects, and asks for balanced, locked, or mostly preferred voice
  gender policy. Generated configs persist the answers in `project_voices` with
  stable enum values and materialized 50/50, 100/0, or 80/20 voice pools for
  `edge-tts` and `kokoro`.
- `/tts:config` — view or change any setting by dotted key
  (`/tts:config summarizer.soft_tokens 220`), with validation against each key's
  own `_options`/`_range`/type hints and a `config.json.bak` backup before
  writing. Restarts the daemon to apply, except changes confined to
  `feature_flags` (re-read per request, no restart). Complements `/tts:voice`,
  which stays the friendlier path for `voice.engine`/`voice.name`.

## [0.1.6] — 2026-06-26

### Fixed
- Summaries no longer get cut off mid-sentence. The summarizer's output-token
  budget moved from a flat 120 cap to a soft target (200) plus a slack cushion
  (96) for a 296-token hard cutoff (`num_predict`), and the inner timeout was
  raised 3.5→5.0s so a full-budget generation completes instead of timing out.
  Because `num_predict` is a ceiling rather than a target, short summaries still
  stop early and pay no extra latency. A completeness backstop trims any runaway
  to its last full sentence (or falls back to the deterministic summary) rather
  than speaking a dangling fragment, and the binary SPEAK/SKIP judge keeps its
  own tiny 16-token budget. +10 tests in `tests/test_summarizer_echo.py`.
- A chunk that fails synthesis no longer strands the rest of an utterance.
  `GenerateStage.generate`'s ordered-yield loop now advances past a failed
  (`None`) chunk and still plays the good tail, fixing a head-of-line stall where
  the listener heard the head of a multi-sentence utterance and then silence.
  New `tests/test_generate_ordering.py`.

### Added
- Number and unit expansion for natural speech: `~1.1s` → "about 1.1 seconds",
  `150ms` → "150 milliseconds", `24.0%` → "24.0 percent" (singular `1s` → "1
  second"). Applied as a final render step in `ProcessStage`, strictly **after**
  the speakability gate, so a pure numeric dump is still dropped rather than read
  aloud. Guards meaning-changing false positives: decades (`the 90s`, `2020s`),
  informal plurals (`100s of`), identifiers/`key=value` (`timeout_30s`), compound
  tokens (`3m24s`), and letter-glued percents (`100%CPU`). +11 corpus contracts.
- `/tts:doctor` now surfaces the summarizer budget and **warns when
  `inner_timeout_s` is too small for the `soft + slack` token budget at the
  model's measured throughput** — a read-only check that catches a budget raised
  past its timeout (or a host too slow for it) before it silently falls back to
  the rule-based summary.

## [0.1.5] — 2026-06-25

### Added
- Sub-agent following is back, now **cwd-scoped** (the safe version of the
  feature deferred in v0.1.4). Each spoken-log entry records the project dir
  (cwd) it was spoken in; the daemon learns a session's cwd from its hook events
  (`spoken_log.note_session_cwd`) and `append` stamps it. `read_merged()` and the
  `/tts:log` merge now only fold in sibling-agent lines whose cwd matches this
  session's, and the statusline `subagent_aware` pivot does the same. Sub-agents
  and background agents inherit their parent's cwd, so they surface; an unrelated
  concurrent session in another directory never does — which is what made the old
  "follow the newest file" pivot mirror two sessions together. Entries that
  predate the cwd field fall back to the old time-only behavior. `read_merged()`
  also accepts an explicit `cwd=` so a caller (e.g. `/tts:log`) can pass its own
  `os.getcwd()`. +3 tests in `tests/test_spoken_log.py`.
- `OLLAMA_HOST` environment variable is now honored for the Ollama endpoint (the
  same var the Ollama CLI/server use). Accepts a full URL or a bare `host:port`
  (scheme assumed `http://`); unset → `http://localhost:11434`. Read at daemon
  start. +1 test.

### Fixed
- Documentation accuracy (caught by an automated doc-vs-code audit):
  `docs/CONFIGURATION.md` dropped a stale `CLAUDE_TTS_LAUNCHD_LABEL` env-var row
  that was never implemented (the launchd label is the fixed
  `com.claude-tts.daemon`); `commands/voice.md` referenced `voice.voice` but the
  daemon reads `voice.name`, so `/tts:voice` now edits the field that actually
  takes effect.

### Changed
- `statusline.subagent_aware` / `active_window_s` / `include_subagent_in_main`
  are no longer "inert/deferred" (v0.1.4) — they are active and cwd-scoped. Docs
  (README, `docs/CONFIGURATION.md`) updated accordingly.

## [0.1.4] — 2026-06-25

### Fixed
- The summarizer no longer speaks its own prompt aloud. The tiny summarizer
  model (`qwen2.5-coder:1.5b`) would occasionally echo the SUMMARIZE rule block
  instead of summarizing ("No 'Here's', no preamble. First person, never 'we'…"),
  and that echo was spoken verbatim. `_call_ollama` now runs an instruction-echo
  detector (`_looks_like_prompt_echo`, ≥2 instruction-only signatures) on the
  model output and falls back to the deterministic rule-based summary on a hit.
  Pure string logic, gated by `tests/test_summarizer_echo.py`.
- Statusline spoken-log is now strictly session-scoped. The previous behavior
  picked the newest spoken-log file in the whole directory to "follow the most
  recently active agent", so two Claude Code sessions running from different
  directories **mirrored each other's spoken output** on the statusline. The
  pivot is removed; the statusline now shows only the current session's own log.
- Harness boilerplate is no longer spoken or repeated. Claude Code appends a
  `Shell cwd was reset to <path>` line to a Bash result when the command ran from
  a different cwd; it rode in on the varying *tail* of stdout, so whole-content
  dedupe missed it and it was spoken several times in a row. It's now stripped at
  the single stdout/stderr chokepoint before classification. A general
  **consecutive-tail guard** also caps utterances that share an identical last
  line at 2 in a row (a different line in between resets it), catching any
  stable-trailing-line noise that whole-content dedupe can't.

### Changed
- Sub-agent *following* on the statusline is deferred until spoken-log entries
  record a cwd/project tag (without it, "follow the newest agent" is
  indistinguishable from "follow an unrelated concurrent session"). The
  `statusline.subagent_aware` / `active_window_s` keys are kept as
  forward-compatible placeholders but are **currently inert**;
  `include_subagent_in_main` still merges by time-overlap across *all* sessions
  and should stay `false` when running multiple projects at once. Docs (README,
  `docs/CONFIGURATION.md`) updated to describe the shipped session-scoped
  behavior accurately.

## [0.1.3] — 2026-06-25

### Fixed
- Turn summaries are no longer filtered as tool-output noise. The assistant's
  end-of-turn summaries were run through the same regex ladder tuned for raw
  command stdout, so ordinary markdown in prose (a `-----` rule, "N files
  changed", a commit SHA) false-positived and silenced the whole summary. The
  drop filter now has a `prose` mode (stop_events) that skips those mid-content
  patterns while still dropping bare code blocks / file paths, duplicates, and
  system reminders. Tool-output filtering is unchanged.

### Added
- Cursor-editor TTS integration: hooks (`cursor-pre-tool-use.sh`,
  `cursor-post-tool-use.sh`, `cursor-after-agent-response.sh`) + a normalizer
  (`cursor_normalize.py`) that map Cursor's hook events to the daemon, plus a
  `CLAUDE_TTS_PASSTHROUGH` stdout gate in the Claude Code hooks (default on →
  unchanged behavior). Note: the after-response hook uses GNU `timeout`
  (preinstalled on Linux; on macOS install coreutils, e.g. `brew install
  coreutils`) for its daemon dispatch.

## [0.1.2] — 2026-06-25

### Added
- Sub-agent / background-agent-aware spoken log. Each sub-agent and background
  agent already gets its own `session_id` from Claude Code, so the per-session
  spoken-log file already separates them. New `spoken_log.read_merged()` folds
  time-overlapping sibling-agent lines into one view, and two view-only config
  flags in the new `statusline` block control it: `subagent_aware` (the
  statusline segment follows whichever agent spoke most recently) and
  `include_subagent_in_main` (`/tts:log` merges sub-agent lines into the main
  view). No daemon changes — purely a display choice.
- Disk guard: `GenerateStage` now checks free space before each synthesis write
  (`min_free_bytes`, 200 MB default). When the cache volume is nearly full it
  evicts aggressively and, if still low, refuses to synthesize and fires a loud
  alert (desktop notification + statusline warning) instead of silently muting
  — fixing the recurring disk-full mute where every engine appears to fail.

### Changed
- CI now runs an informational (non-blocking) async test suite so async pipeline
  regressions are visible, not just the sync `make verify` gate.

## [0.1.1] — 2026-06-25

### Added
- Spoken-output log: the daemon now records every utterance it actually speaks
  to a bounded, per-session JSONL at `~/.claude/logs/tts/spoken/<session>.jsonl`
  (`daemon/spoken_log.py`, best-effort — a logging failure never breaks
  playback), surfaced by the new `/tts:log` command (newest-first, with
  timestamps and category).
- README demo: an animated terminal GIF, a matching audio sample, and a
  combined MP4 (picture + sound), generated by replaying
  `tests/fixtures/event_corpus.jsonl` through the real `ContentRouter`
  (`scripts/demo_gif.py`, `scripts/demo_audio.py`) — the demo is derived from
  the same corpus that gates CI, so it can't drift from behavior.
- `docs/ARCHITECTURE.md` — the filter brain, the three seams, the async
  pipeline, backpressure tiers, and the markdown→speech chokepoint.
- `docs/CONFIGURATION.md` — every config block, code-verified defaults, the
  timeout-clamp invariant, and the full environment-variable table.

### Fixed
- Long-utterance playback cutoff: a long spoken answer (e.g. a commit message
  read at the Stop hook) is no longer truncated mid-read when the next event
  escalates the session to the BLACK drift tier. `flush_buffer` now preserves
  the in-progress utterance's own tail chunks, matched by a persisted
  `PlaybackState.current_request_id`, instead of discarding them as backlog.
- Git SHA-range leak: a revision range `sha1..sha2` now drops BOTH hashes — the
  second was shielded by the dotted-identifier lookbehind and leaked to speech.
- Filename cadence: `name.ext` for a known code/file extension is spoken as
  "name dot ext", so the engine no longer reads the extension dot as a sentence
  end and pauses mid-utterance (reported on "claude-tts.tsx"). Prose, decimals
  ("3.5"), and abbreviations ("e.g.") are left untouched.
- Documentation drift: the README no longer describes `/tts:setup` as a future
  milestone, and the setup skill no longer claims Linux service install is
  unautomated — both shipped in 0.1.0.

## [0.1.0] — 2026-06-25

First packaged release: a local text-to-speech daemon for Claude Code, with
smart content routing, hooks, and graceful degradation.

### Added
- Three swappable seams: LLM provider (`ollama` / OpenAI-compatible / `null`),
  TTS engine (`kokoro` / `voicebox` / `edge-tts` / `say` / `espeak`), and OS
  platform (audio playback + service install).
- Zero-dependency fallback engine: macOS `say` / Linux `espeak`, so TTS works
  with no ML or network dependencies.
- Cross-platform audio playback: macOS `afplay`; Linux auto-detects
  `ffplay`/`mpv`/`pw-play`/`paplay`/`aplay` (decoders first, for format safety).
- Background service install: macOS `launchd`, Linux `systemd --user`
  (`enable --now` + `loginctl enable-linger`). Windows points to WSL2/Docker.
- Commands: `/tts:setup` (mini-eval calibration + service install + config),
  `/tts:doctor` (idempotent health checks), `/tts:uninstall`.
- Binding quality gate (`make verify`) — deterministic, no live deps.
- CI matrix (GitHub Actions): macOS + Linux × Python 3.11–3.13.
- Pinned dependencies (`uv.lock`) and version-synced manifests
  (`make manifests`).

### Notes
- Volume control is macOS-only (`afplay -v`); on Linux the audio daemon owns
  system volume.
- Windows service install and Linux/Windows volume are not yet implemented.
