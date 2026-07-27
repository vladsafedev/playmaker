# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-07-27

### Changed

- **Sub-agent permissions are configurable, and Claude no longer skips them by
  default.** 0.4 passed `--dangerously-skip-permissions` to every Claude
  dispatch because the alternative was believed to be a run that stalls on the
  first prompt. That is not what happens: a headless `claude -p` without
  permissions answers "I need your permission" and returns immediately, having
  changed nothing. There is also a middle tier, verified against claude 2.x:
  `--permission-mode acceptEdits` lets the agent edit files and run commands
  inside the dispatch `--cwd` while claude itself refuses anything outside it.

  That is the new default. `[agents.claude]` now accepts `permission_mode`,
  `allowed_tools` and `disallowed_tools`, and `yolo = true` restores the old
  no-boundary behaviour in one line. The 0.4 spelling `skip_permissions = true`
  is still honoured; `skip_permissions = false` now falls through to the
  default mode instead of producing a run that does nothing.

  **Upgrade note:** a subtask that needs to write outside its `--cwd` will now
  be refused. Point `--cwd` at the right directory, or set `yolo = true`.

### Added

- `[agents.codex] sandbox` — forwarded to `codex exec -s`
  (`read-only` / `workspace-write` / `danger-full-access`). Codex needs no
  permission-skipping flag: `codex exec` is already non-interactive and
  sandboxes the model's shell itself, and playmaker never passed it one.
- `[agents.agy] sandbox` — forwarded to `agy --sandbox`. agy has no per-mode
  permission flag, so it keeps `yolo = true` as its default.

## [0.4.0] - 2026-07-27

### Added

- `playmaker skill install` — the `playmaker-coach` Claude Code skill now ships
  with the package (`skills/playmaker-coach/SKILL.md`) and installs itself into
  `~/.claude/skills/`. It was previously documented as a separate repository
  that was never published.
- `playmaker --version`. `__version__` is read from installed package metadata
  rather than a hand-maintained literal, which had drifted to 0.1.0.
- `[notifications] editor` in `config.toml` — the app a clicked notification
  opens the output in was hardcoded to Zed.

### Fixed

- `claude`: a failing `claude -p` run reports the error in its final
  stream-json event with an empty stderr, so failures surfaced as a blank
  `claude failed (exit 1):`. The error text is now captured and raised.
- `codex`: a non-zero exit no longer discards an answer the CLI had already
  written. The last-message file is consumed before the exit-code check, so
  testing the file at that point always failed — codex's harmless
  "failed to record rollout items" shutdown warning would have lost the result.
- `codex`: invalid-model / auth / upstream failures are now surfaced. Codex
  reports these via a `turn.failed`/`error` stream event while still exiting 0
  and writing an empty last-message file, so a bad `--model` used to look "done"
  with no output. The handler now inspects those events (unwrapping the nested
  JSON error) and raises, so the coach reroutes instead of silently getting
  nothing back.

### Added

- `agy`: `--model` is validated against `agy models` before dispatch. agy
  resolves an unknown model name to its default *silently* (wrong model runs, no
  error); a typo'd or stale name now raises with the available roster. Skipped
  when the roster can't be read, so a probe hiccup never blocks a dispatch.
- Antigravity quota probe now prefers agy's **local daemon** (`RetrieveUser
  QuotaSummary` over the embedded self-signed-TLS gRPC-web endpoint), giving the
  full categorized breakdown — Gemini and Claude/GPT, each split 5-hour vs
  weekly — matching Antigravity's own UI and CodexBar. Works whenever any agy or
  CodexBar daemon is running. Falls back to the coarse OAuth `retrieveUserQuota`
  (Gemini daily buckets only) with a "daemon offline" note when none is up.
  Ported from steipete/CodexBar's `AntigravityStatusProbe`.
- Quota render: adaptive name column so longer categorized labels keep bar
  alignment.

### Changed

- Packaging: PEP 639 license metadata, accurate description/keywords, sdist now
  ships tests and the skill.
- Lint: ruff `select = E,F,I,UP,B` (minus B008, which flags Typer's own API) and
  linting covers `tests/` too.
- Removed `packaging/homebrew/playmaker.rb` — it targeted a tap that was never
  published, under an org name no longer in use.

## [0.3.0] - 2026-07-12

### Added

- `agy` agent handler — Antigravity CLI (Google AI Pro): Gemini 3.5 Flash / 3.1 Pro,
  Claude Sonnet/Opus 4.6 (Thinking), GPT-OSS 120B, addressed by display name from
  `agy models`. Oneshot via `agy -p`, resume via `--conversation <id>`; the
  conversation id is recovered early from the CLI debug log (`--log-file`), and
  threads are parsed from the plain-JSONL brain transcript
  (`~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript_full.jsonl`).
  Dispatch prepends a workspace preamble because the agy agent's shell cwd is a
  private scratch dir — without it, relative file writes never reach the workspace.
- `antigravity_probe` quota probe (daily-cloudcode-pa, ideType ANTIGRAVITY) —
  replaces the gemini probe in the default set; surfaces Gemini buckets only
  (Antigravity's Claude/GPT windows are not exposed to plain OAuth tokens).

### Changed

- Default probe set is now codex / claude / agy; gemini-cli's handler and probe
  remain in the codebase but the CLI is retired locally.

## [0.2.0] - 2026-06-10

### Removed

- ACP/Zed integration layer — project is now a dispatch-only core with no Zed or ACP dependencies.

### Changed

- `claude`: headless dispatch and resume now pass `--dangerously-skip-permissions` so unattended runs are not blocked by permission prompts.
- `quotas`: CLI renders the Extra usage pool (metered overage) alongside standard quota display.
- `gemini`: falls back to session-file parse when the stream or JSON response body is empty, preventing silent failures on partial responses.
