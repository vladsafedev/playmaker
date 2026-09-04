# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.0] - 2026-09-04

### Added

- **`playmaker quotas` shows the two buckets that actually run out first.**
  Codex's `additional_rate_limits[]` carries a separate GPT-5.3-Codex-Spark
  pool with its own 5h and weekly windows, and Anthropic's `limits[]` carries
  a model-scoped weekly next to the all-models one. Both were in the raw
  responses and dropped by the probe, so a coach could route a junior WP to
  Spark while its weekly sat at 4%, or budget a session on the all-models bar
  while its own model-scoped bucket was the one about to end it. Codex now
  renders a `Codex — Spark` sub-block and Claude a `Weekly · <model>` row;
  probes return an additive `blocks` key that `_render_provider` draws for any
  provider.

### Changed

- **The coach no longer tiers Antigravity's Claude models by name.**
  `lanes.md` called `agy`'s Claude-Opus "top-tier Claude judgment on Google's
  pool" and offered it as the reviewer to reach for when the Anthropic bucket
  is precious. That roster trails Anthropic's releases by a generation, and
  the name kept pulling real work onto it. The reference now says to tier by
  the version `agy models` shows, and points the second-strong-reviewer row
  at Gemini Pro.

## [0.9.0] - 2026-08-24

### Added

- **`playmaker quotas` keeps itself fresh.** The snapshot in `quotas.json` is
  re-probed automatically once it is older than `[quotas] max_age` (default
  5m), and the header now prints how old the numbers are. A stale table was
  worse than no table: the coach quotes it when it decides where to send work,
  and nothing warned that "62% left" was four days old. `--refresh` still
  probes on demand and `--cached` prints the stored snapshot without probing.

- **`skill install` copies the whole skill directory.** The bundled skill has
  outgrown a single file — `references/` the coach reads on demand, `scripts/`
  it runs — and install only ever copied `SKILL.md`, so none of that reached
  `~/.claude/skills`. It now walks the bundle, keeps the executable bit on
  scripts, and deletes nothing the bundle does not own, so `--force` is an
  upgrade rather than a reset of whatever you keep alongside it.

- **The coach skill is a protocol plus references, and it ships a review board.**
  `SKILL.md` was a 315-line monolith that cost 35 KB of context to activate,
  which is its own argument against activating it. It is now a ~200-line
  protocol, with the lanes, quota rules, per-agent traps, prompt templates and
  command surface moved into `references/` and read on demand.

- **`scripts/review-board.sh` — automatic multi-agent review of a work package.**
  It snapshots the WP's diff, builds one refute-the-implementation prompt per
  reviewer (distinct lens each: correctness, contracts, risk, conventions),
  dispatches them `--read-only` under a single `--batch`, and collects their
  verdicts as JSON with `--collect`. The roster comes from
  `.playmaker/reviewers.conf` (repo) or `~/.playmaker/reviewers.conf` (global),
  keyed by risk class, so which lanes review what is configuration rather than
  something the coach improvises each round. The implementing lane is excluded
  with `--impl-agent`.

- **Policy overlay.** The coach reads `./.playmaker/policy.md` then
  `~/.playmaker/policy.md` before planning, and lets them override the skill's
  defaults — which quotas to spare, which lanes are contended, what juniors may
  never touch in this repo, which commands are the acceptance gates. Personal
  routing rules used to survive only as local edits to the installed
  `SKILL.md`, which made upgrading the bundled skill destructive.

- **Write-task no-change detection.** Every dispatch and continuation now takes
  a before/after working-tree snapshot: git directories compare porcelain
  state plus `HEAD`, while ordinary directories use a bounded mtime walk. A
  successful write task that changed zero paths is stored as `no_changes`,
  with the count and snapshot hashes available from `get`/`summary` and JSON.
  `--expect-changes` forces the check; `--read-only` suppresses it for recon
  and answer-only work.

### Changed

- **The skill's activation threshold is much lower.** It used to require 3+
  independent work-streams and a >2x parallel speedup, which read as "not this
  task" for most real requests; it now activates on any code change spanning
  more than one file, anything that deserves an independent review pass, or any
  request with two parallelizable parts — a single reviewed work package is an
  expected shape, not overhead.

- **`no_changes` is terminal but not success.** It appears as a warning in
  watch and list filtering, pings immediately with the failure sound even in
  a batch, and makes the batch report its agent as `⚠ no_changes` rather than
  counting it among the completed workers.

- **`summary --json` now wraps turns in `summary`.** Snapshot diagnostics share
  the object with the former bare turn list, so JSON consumers should read
  `summary` for the messages.

## [0.8.0] - 2026-08-18

### Added

- **Ollama in the quota table.** `playmaker quotas` now carries an `ollama`
  provider for work dispatched through `opencode -m ollama/<tag>`. It is an
  availability signal rather than a quota: `ok` at 100% with the pulled chat
  models listed when the daemon is up and a completion-capable model is
  present; `unsupported` — naming the exact `ollama pull` to run — when Ollama
  is down or holds only embedding models, so an idle daemon with
  `nomic-embed-text` never reads as free 27B capacity. Models are classified by
  `/api/show` `capabilities`, not by name. The renderer for the provider
  shipped in 0.7.2 ahead of the probe.

## [0.7.2] - 2026-08-18

### Fixed

- **`[agents.<name>] binary` was documented, written into every config `init`
  produced, and read by nothing.** All five handlers hardcoded the executable —
  `shutil.which("opencode")` for the availability check, `cmd = ["opencode", …]`
  for the dispatch — so a config pointing at an absolute path or an alternate
  build changed exactly nothing, and the agent still had to be on `PATH` under
  its own name. That is the wrong assumption for how these CLIs install:
  opencode lands in `~/.opencode/bin`, which reaches `PATH` only through a line
  in an interactive `.zshrc`, so every non-interactive dispatch — cron, an
  editor-spawned run, the coach itself — reported the agent unavailable unless
  you'd worked around it with a symlink. The setting is now honoured everywhere
  an executable is named: `is_available()`, both dispatch and resume, and the
  roster probes (`agy models`, `opencode models`, `gemini --list-sessions`). A
  bare name still resolves on `PATH`; an absolute path is used as-is and a
  leading `~` is expanded, since subprocess does no shell expansion of its own.
  When the lookup fails, the error now names the executable it actually tried
  and where the setting came from, instead of insisting it is "not on PATH".

- **The Z.ai windows lost their names when z.ai renamed them.** `playmaker
  quotas` had started printing the GLM rows as `5 hours` and `1 week` instead
  of `Session` and `Weekly`: the plans moved to weekly Credits and the API now
  types the inference windows `CREDIT_LIMIT` where it sent `TOKENS_LIMIT` in
  July, so the label lookup missed and fell back to rendering the bare span.
  The labels are shared with the claude probe on purpose — the two providers
  are meant to read like-for-like down the table — so the fallback quietly cost
  the comparison. Both spellings are mapped now. The monthly `MCP tools` pool
  is simply absent from the response on a current plan; nothing to do, it just
  stops appearing.

- **The coach skill named a stale GLM model and lied about where the opencode
  default lives.** The skill ships in the wheel, so its facts are the coach's
  facts. It routed bulk work to `zai-coding-plan/glm-5.2` when the plan's
  current flagship is `glm-5.3`, described the Z.ai quota as token windows plus
  a monthly `MCP tools` pool (the windows are credits now; the pool is gone),
  and told the coach that omitting `--model` falls back to whatever
  `~/.config/opencode/opencode.json` names. That last one sends you looking in
  the wrong file: opencode keeps the interactively-picked default in its own
  state, not in that config, so a machine with no `model` key there still
  resolves to something — and the only way to see what an unqualified dispatch
  will run is the `providerID`/`modelID` on a past session in `opencode.db`.

- **`--model` for agy validated against the wrong column.** agy 1.1.14 prints
  `agy models` as `<slug>\t<Display Name>` after a `Fetching available
  models...` line; the roster was read as whole lines, so every real slug was
  rejected as unknown. Only the first token counts now (older builds printed
  the bare slug — same first token).

## [0.7.1] - 2026-08-18

### Fixed

- **The Antigravity quota probe knocked on every port on the machine, and one
  of them answered in TLS.** `playmaker quotas` had shown
  `Antigravity (agy) error: BadStatusLine` for a week: the daemon lookup ran
  `lsof -p <pid> -iTCP -sTCP:LISTEN` without `-a`, and lsof ORs its selectors
  unless told otherwise — so "agy's listening sockets" was actually every
  LISTEN socket on the box (hence the old `!= 5432` Postgres carve-out). The
  probe then POSTed the quota RPC to Steam, chromedriver, a Logi plugin, …
  until a TLS-only listener sorted below agy's ports answered a plaintext
  request with a TLS alert record. urllib raises that as
  `http.client.BadStatusLine`, which is not an `OSError`, so it escaped the
  per-port `except`, escaped `antigravity_probe`, and the aggregator recorded
  the whole provider as an error — with the working local daemon two ports
  away. lsof now gets `-a` (one call, all pids), the `pgrep` for `agy` is
  anchored so playmaker's own dispatches with `playmaker-agy-*.log` in their
  arguments don't count as the daemon, anything a port says only disqualifies
  that port, and any failure on the local path falls back to the remote
  Gemini-only probe rather than to `error`. The refresh also stopped spending
  ~30 s in timeouts on strangers' ports (1.3 s now).

## [0.7.0] - 2026-08-05

### Fixed

- **A batch label is a name you reuse, so the summary now belongs to the
  fan-out rather than the label.** `--batch dashboard` twice used to mean one
  batch forever: the second fan-out never pinged at all, because exactly-once
  was guarded by an `O_EXCL` sentinel in `logs/` that nothing ever removed —
  and with the sentinel gone it would have counted yesterday's sessions too,
  `4/4 done · codex ✓ · claude ✓ · codex ✓ · agy ✓` for a fan-out of two, with
  the stale outputs pasted into the combined `/tmp` file. The claim now lives in
  `state.db` as a `batch_notified` column: `list_batch` returns the sessions not
  yet reported, and the finisher that wins the claim releases the label for the
  next fan-out. Cross-process safety is unchanged — the loser's `UPDATE` finds
  its set already claimed, rolls back and stays quiet.

- **`playmaker kill` drains the batch it empties.** `killed` is terminal like
  `done` and `failed`, but `kill` never finalised, so killing a fan-out's last
  live session left nobody to notice the batch had drained: no summary, ever.

### Changed

- **The combined batch file moved out of `/tmp`.** It is what a batch
  notification opens on click, so it now lands with the outputs it quotes —
  `~/.playmaker/outputs/batch-<label>.md` rather than
  `/tmp/playmaker-batch-<label>.md`, a predictable name in a world-writable
  directory built from a label the user chose. It also meant the test suite
  wrote outside `tmp_path`.

## [0.6.0] - 2026-07-27

### Added

- **`opencode` agent handler — one lane, ~75 providers.** The ask was z.ai GLM
  support, but playmaker never talks to a model API: it drives agent CLIs. So
  rather than teach the claude handler an `ANTHROPIC_BASE_URL` override — which
  would have *replaced* the Anthropic lane rather than added one — the handler
  wraps [opencode](https://opencode.ai), whose `--model provider/model` already
  reaches GLM (`zai-coding-plan/glm-5.2`), local LMStudio/MLX models, and
  everything else on models.dev. Oneshot via `opencode run --format json`,
  resume via `-s <id>`; every JSONL event carries `sessionID`, so a detached
  dispatch records the id from the first line rather than at completion.

  Four behaviours worth knowing. opencode resolves its working directory from
  `process.env.PWD`, which `subprocess.Popen(cwd=…)` does not update — left
  alone it ignores `--cwd` and writes into whatever directory the coach was
  sitting in, so the handler passes `--dir` and corrects `PWD`.
  opencode ≥1.18 keeps transcripts in SQLite
  (`opencode.db`), not one file per session, so playmaker writes a pointer at
  `~/.playmaker/opencode/<id>.session` and reads the `session`/`message`/`part`
  tables live — which keeps `thread --follow` current rather than frozen.
  Cost comes from opencode's own per-session accounting rather than the stream,
  because `run --format json` can exit before its final `step_finish`
  ([opencode#26855](https://github.com/anomalyco/opencode/issues/26855)).
  And `[agents.opencode] model` matters more than the usual `--model` default:
  left unset, opencode falls back to whatever its own `opencode.json` says,
  which is typically the model you last picked interactively rather than the
  one you meant to dispatch to.

  Permissions follow agy, for the same reason: `--auto` is opencode's only
  lever, so a detached run either auto-approves or comes back having done
  nothing. `yolo` defaults to `true`; narrow it with the `permission` block in
  your own opencode.json, which `--auto` still honours for denies.
- **`zai` quota probe** — GLM Coding Plan windows (5-hour, weekly, and the
  monthly MCP tool pool) from `api.z.ai/api/monitor/usage/quota/limit`, keyed
  off the credential opencode already wrote to its `auth.json`, so playmaker
  still stores no secret of its own. Reported as its own provider rather than
  under `opencode`, because the quota belongs to the plan — an opencode lane
  pointed at a local model spends nothing here. Degrades to *unsupported*, not
  an error row, when no Z.ai credential exists.

## [0.5.1] - 2026-07-27

### Fixed

- **The bundled coach skill taught agy model names agy no longer accepts.**
  `agy models` switched from quoted display strings (`"Claude Opus 4.6
  (Thinking)"`) to bare slugs (`claude-opus-4-6-thinking`), so every agy
  dispatch the skill proposed failed model validation. The skill now says to
  read the live roster from `agy models` instead of spelling names from
  memory. It also recommended `codex -m gpt-5-codex`, which fails on accounts
  whose plan lacks that model; codex dispatches now default to the account's
  own default model. Both fixes landed on main just after v0.5.0 was tagged —
  this release exists because the skill ships inside the wheel, so doc fixes
  are not live until published.

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
