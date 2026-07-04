---
name: tts:config
description: View or change any claude-tts setting by key, with validation, then restart the daemon to apply.
---

View or change **claude-tts** settings. This edits the same config `/tts:setup`
writes and `/tts:voice` edits — it just covers every block, not only `voice`.
The annotated schema (every key + what it does) lives in `config.example.json`.

Config file: `~/.config/claude-tts/config.json`.
Argument (optional): `$ARGUMENTS` — a dotted key, optionally followed by a value.

Steps:

1. Read `~/.config/claude-tts/config.json`. If it does not exist, tell the user
   to run `/tts:setup` first and stop. Keys beginning with `_` are documentation
   (comments, option lists, ranges) — never show them as settings, but **do**
   read them as hints (below).

2. **No argument** → print the current settings grouped by block (`voice`,
   `filtering`, `summarizer`, `routing`, `statusline`, `audio`, `performance`,
   `logging`, `feature_flags`, and any others present). Show each real key's
   value; suppress the `_`-prefixed meta keys. After the listing, note that the
   annotated reference is `config.example.json`, and that `/tts:voice` is the
   friendlier path for `voice.engine`/`voice.name`.

3. **A dotted key, no value** (e.g. `summarizer.soft_tokens`) → show that key's
   current value and, if a sibling hint exists, its meaning: look for
   `_<key>_desc`, `_<key>_options`, `_<key>_range`, or a block `_comment`.

4. **A dotted key and a value** (e.g. `voice.rate 1.1`,
   `feature_flags.legacy_speak_enabled false`) → set it:
   - **Delegate voice** — if the key is `voice.engine` or `voice.name`, stop and
     run `/tts:voice` instead; it knows engine↔voice compatibility and speaks a
     confirmation. Don't edit those here.
   - **Validate** against the key's own hints, in this order: an
     `_<key>_options` / `_levels` list → the value must be one of them; an
     `_<key>_range` → within it; otherwise the value must match the JSON **type**
     of the current value (bool stays bool, number stays number, string stays
     string). Reject with the allowed set/range/type if it doesn't fit — do not
     write an invalid value.
   - **Back up** the current file to `config.json.bak` (same dir), then write the
     change. Preserve every other key **exactly**, including all `_`-prefixed
     documentation keys, and emit valid JSON (2-space indent, like the original).
   - If the key does not exist in the file, say so and point to
     `config.example.json` rather than inventing a new key — only offer to add it
     if the example documents it.

5. **Apply.** The daemon reads config **at start**, except the `feature_flags`
   block, which is re-read per request. So:
   - If the only change was under `feature_flags`, it takes effect immediately —
     no restart.
   - Otherwise restart the daemon so the change applies, and confirm the socket
     is bound afterward at
     `${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}`.

6. If you changed a `summarizer` budget key (`soft_tokens`, `slack_tokens`,
   `inner_timeout_s`), suggest `/tts:doctor` — its budget-vs-timeout check
   verifies a full-budget summary still completes before the inner timeout.

7. Speak a one-line confirmation of what changed (or, for a read, keep it silent
   — a view changes nothing).

Do not hardcode absolute personal paths; read locations from `~/.config/claude-tts`
and the XDG socket expression above. If the daemon is not installed, say so and
suggest `/tts:setup`.
