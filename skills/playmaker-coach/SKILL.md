---
name: playmaker-coach
description: Playing-coach orchestration of Claude/Codex/Antigravity (agy)/opencode sub-agents via the `playmaker` CLI and in-session Task sub-agents. Triggered when the user gives a complex multi-component task where decomposition gives >2x parallel speedup (e.g. "build admin dashboard with schema, backend, FE, tests, docs"). NOT triggered for single-file refactor, single bug fix, single component, or simple Q&A — those stay in this thread without delegation.
---

# playmaker-coach — playing-coach orchestration

Use the `playmaker` CLI as a facade to dispatch sub-tasks to Codex / Antigravity (`agy`) / opencode / a sibling Claude, monitor them, read their threads, review their diffs, and feed back. The coach (this thread) does its own portion of the work in parallel.

> **`agy` (Antigravity CLI)** does not only serve Google models: alongside the Gemini Flash and Pro tiers, `agy models` carries **Claude Sonnet and Opus** and a **GPT-OSS** mid-tier. Opus via agy runs on *Google's* quota pool — top-tier Claude work that does not touch the Anthropic subscription's scarce Opus weekly bucket: `--model claude-opus-4-6-thinking`.
>
> **Never write an agy model name from memory — run `agy models` and copy a line.** Both the roster *and its spelling* move with Antigravity releases: names used to be quoted display strings like `"Claude Opus 4.6 (Thinking)"` and are now bare slugs. playmaker validates `--model` against the live roster and fails the dispatch on a stale name, so a wrong guess costs you a round-trip.

**The point of the coach pattern:** all available models do useful work in parallel under your direction. The coach's job is *orchestration*, not *production*. A coach that does its own implementation, runs its own codebase recon, and reviews everything line by line burns the same budget delegation was meant to save. Treat your own context window as the most expensive resource on the table — every byte you read or generate yourself is a byte that could have come from a cheaper model. Push out as much as you can: implementation, recon, summarization, even drafting the per-subtask prompts when the task is large enough.

## Execution lanes — where work runs

All Claude work — coach, internal sub-agents, external `claude -p` — draws from the **same Claude subscription**. So routing is about **which weekly bucket** you spend and **where results land**, not who pays. Three lanes:

1. **Coach (this thread).** Interactive Claude Code on the **Opus** weekly bucket — the scarcest, hardest-to-replenish one. Serial and the most expensive in *context*. Reserve for orchestration, architecture judgment, final integration.

2. **Internal sub-agents — the `Task`/`Agent` tool in THIS session.** Run inside this session, **write files** (they inherit the coach's permission mode), return their result straight into the coach's context, and run in parallel. They draw on the same subscription, so the gate is "is weekly quota healthy?" — glance at `playmaker quotas`. **Use them freely** for Claude-side chunks the coach will fold back in directly; spawn several at once, don't be shy.

3. **External dispatch — `playmaker dispatch <agent>`.** Separate OS processes, tracked in playmaker (`list`/`watch`/`thread`/`continue`):
   - **`claude -p` (sibling Claude):** same subscription. Its key lever is the model bucket — **Sonnet is a separate weekly bucket from Opus**, usually idle while Opus depletes, so default **`--model sonnet`** to spare the scarce Opus bucket (**`--model haiku`** for trivial mechanical work). It **can write files** — playmaker runs it in `acceptEdits`, so it edits and runs commands freely inside `--cwd` and is refused outside it (see §10). Use it over an internal sub-agent when you want a **tracked, detached work-stream** you can monitor/continue independently of the coach's turn.
   - **`codex` / `agy` / `opencode`:** each on its own subscription/quota — the right home for **write-heavy** parallel implementation that can leave the Anthropic subscription. `agy` is special: besides Gemini tiers it carries **Claude Sonnet/Opus 4.6 (Thinking)** on Google's pool, so even "must be Claude-quality" work can leave the Anthropic quota. `opencode` is the widest lane: one CLI over ~75 providers, addressed as `provider/model` — a GLM coding plan (`zai-coding-plan/glm-5.3`), or a model running locally on this machine, which spends **no** subscription quota at all.

**Routing cheat-sheet for Claude-side work:**

| The subtask… | Lane | Why |
|---|---|---|
| writes files and the coach integrates the result directly | **internal sub-agent** (Task tool) | in-session, write-capable, returns into context |
| is an independent stream you'll monitor / continue separately | **external `dispatch claude --model sonnet`** | tracked & detached; Sonnet's own weekly bucket spares Opus |
| is heavy reasoning only the coach can do | **coach** | top-tier Opus, serial |
| is write-heavy and can leave Claude | **codex / agy / opencode** | their own quotas |
| needs top-tier Claude but the Anthropic Opus weekly is precious | **`dispatch agy --model claude-opus-4-6-thinking`** | Opus quality on Google's pool |
| is bulk work and every subscription is running low | **`dispatch opencode --model zai-coding-plan/glm-5.3`** | a separate GLM plan, untouched by the others |
| is mechanical and privacy-sensitive, or all quotas are spent | **`dispatch opencode --model <local provider>/<model>`** | runs on this machine; costs no quota, just wall-clock |

**Sonnet is your cheap parallel Claude worker** — a separate weekly bucket that usually sits idle while Opus depletes. Reach for it (via `dispatch claude --model sonnet`, or by pointing an internal sub-agent at Sonnet) instead of burning Opus on mid-tier work.

## Activation

**Activate when** the task has 3+ independent work-streams and parallel execution would meaningfully shorten the wall-clock. Typical: backend + frontend + tests + docs; or schema + handlers + tests; or several disjoint modules.

**Do NOT activate** for: single bug fix, single-file refactor, naming change, simple question, single component. The coach overhead is not worth it; just do the work in this thread.

If unsure, ask the user briefly: "This looks like a multi-component task — want me to delegate parts to Codex/agy in parallel, or handle it myself?"

## Protocol

### 1. Reconnaissance

```bash
playmaker agents                    # who is installed and reachable?
playmaker quotas --refresh          # current capacity, broken out per model
```

**Why we pull quotas:** to deliberately offload work *away from* the most-depleted models and *toward* the freshest. The coach is Claude (top-tier), and the top-tier weekly bucket is the scarcest, hardest-to-replenish resource — every chunk the coach handles itself burns it. So the quota table is not a passive health check; it is the load-balancing input to step 3.

**Read the table at model granularity, not provider granularity.** Each provider exposes multiple tiers and they have separate quotas:

- Claude: two non-coach ways to run it — an **internal sub-agent** (Task tool; in-session, write-capable, result returns to the coach) and an **external `claude -p` dispatch** (tracked, detached stream). Both draw on the subscription; the difference is where results land, not cost. See "Execution lanes". Default external Claude to `--model sonnet` — its weekly bucket is separate from Opus and usually idle, so it spares the scarce coach (Opus) bucket.
- Antigravity (`agy`): one Google pool split across model families — Gemini Flash tiers for cheap bulk work, Gemini 3.1 Pro for hard Gemini work, **Claude Sonnet/Opus 4.6 (Thinking)** as Anthropic-quality capacity that spends *Google's* quota, GPT-OSS 120B as a spare mid-tier. `playmaker quotas` shows the **full categorized breakdown** — `Gemini 5h` / `Gemini weekly` and `Claude/GPT 5h` / `Claude/GPT weekly`. Two things share a bucket: all Gemini models draw the Gemini bucket, and Claude *and* GPT-OSS share the Claude/GPT bucket. So dispatching Opus 4.6 via agy spends the same `Claude/GPT` bucket as Sonnet or GPT-OSS — watch the `Claude/GPT 5h` window when fanning out several agy-Claude jobs. (This needs agy's local daemon up — normally true when any agy process is running; if `playmaker quotas` tags agy "daemon offline" it fell back to a coarse Gemini-only view.)
- Codex: top-tier vs lighter modes (where applicable).
- Z.ai (GLM): shown as its own provider because the plan is what has the quota, not the CLI — `Session` (5h) and `Weekly` windows, denominated in the plan's **credits** (Pro: 12k per session, 60k per week). It appears whenever `opencode auth login` has a Z.AI credential, and reads *unsupported* when it doesn't. An `opencode` dispatch pointed at a **local** model spends none of it, so local lanes never show up in this table at all.

If `quotas.json` is more than 1h old (or shows errors), say so before relying on the numbers.

### 1a. Delegate the recon, not just the implementation

Codebase exploration ("find where the User entity lives, list its existing fields, locate the auth middleware") is itself delegable — and one of the highest-leverage things to delegate, because raw reading is exactly what burns coach context cheapest models can do. Before doing your own `grep`/`Read` sweep, ask: *can a Flash / Sonnet model produce the answer I need in a short structured report?*

Pattern: dispatch a recon subtask with an explicit deliverable.

```bash
playmaker dispatch agy --model gemini-3.6-flash-low --cwd $(pwd) --sync \
  --prompt "Recon only — do not edit any files. In apps/backend/, locate: (a) the User entity/schema and the migration tooling used (Prisma vs TypeORM vs other), (b) the auth middleware that resolves the current user, (c) where DTOs are defined for user PATCH endpoints if any. Report under 200 words as a numbered list with file paths and line ranges."
```

Use `--sync` here so the report comes back inline; the coach reads ~200 words instead of skimming dozens of files. Cost: a fraction of doing it yourself. The coach uses that report to write the *implementation* prompts.

Don't over-use this for trivial recon (one file, one symbol — just `Grep` it). The win is when recon would otherwise span many files.

### 2. Load profiles

Read `./.playmaker/agents/*.md` first (project-local override), fall back to `~/.playmaker/agents/*.md` (global). The bodies describe each agent's strengths, weaknesses, and when to delegate to them. Match these against the task's subtasks.

### 3. Decompose and propose a plan

Break the task into 2-5 subtasks (don't go finer-grained than that on first run — too much coordination overhead). For each subtask, name the agent and state why.

**Load-distribution heuristic** (apply on top of profile fit):

1. Sort *models* (not agents) by remaining capacity — freshest at the top. A provider with one fresh model and one depleted model is two separate buckets.
2. **Tier-match each subtask to the cheapest model that can finish it cleanly:**
   - **Architectural / spec judgment / cross-module integration:** top tier (Opus, agy `claude-opus-4-6-thinking` / `gemini-3.1-pro-high`, Codex top-tier). The coach lives here; agy-Opus is the overflow lane when the Anthropic Opus weekly is precious.
   - **Pattern-following implementation, well-scoped CRUD, mechanical refactor, test scaffolding, writing inside an existing convention:** mid tier (Claude Sonnet, agy `claude-sonnet-4-6` / `gemini-3.5-flash-high` / `gemini-3.1-pro-low`, mid-tier Codex). This is where the bulk of delegated implementation goes.
   - **Recon, summarization, mechanical loops over many files, name normalization:** cheap tier (agy's lowest Flash tiers, **Claude Haiku**, Sonnet for recon-with-judgment).

   For Claude specifically, tier is orthogonal to **lane** (see "Execution lanes"): pick the model tier here, then decide *internal sub-agent* (coach integrates the result) vs *external `-p`* (tracked detached stream).
3. Pass the model explicitly: `playmaker dispatch <agent> --model <name> ...`. Without `--model`, the agent CLI uses its default — which is usually fine but means the coach gives up control over tier-matching. **For sibling Claude, default to `--model sonnet`** (Haiku for trivial mechanical work); never omit it and let the CLI pick Opus — that burns the scarce shared Opus weekly bucket.
4. Reserve the most-depleted top-tier model for the lightest role — usually the coach's own coordination, glue, and final integration. Mid- and cheap-tier work goes to mid- and cheap-tier models, even if the depleted top-tier model could technically do it.
5. If the coach's own weekly is below ~50%, aggressively shrink its slice: design decisions and final integration only. Push everything else — implementation, recon, prompt-drafting for sub-subtasks — to lower tiers.
6. State per-model capacity in the plan proposal (not per-agent), so the user sees the rationale and can correct the routing.

**Sizing rule (critical — otherwise the coach saves nothing):**

Delegation only pays off when the subtask is **finishable without coach supervision**. If the agent needs multiple rounds of "no, that's wrong, try again," the coach burns Claude tokens reviewing — exactly the budget delegation was meant to save. Two mistakes here defeat the whole pattern:

- **Subtask too big or underspecified** → agent rambles or solves the wrong problem; coach reviews heavily, re-prompts repeatedly. Net: coach spent more than just doing it.
- **Subtask above the agent's ceiling** → agent flails on something it can't actually do (architectural judgment it lacks context for, integration across modules it can't see, novel API design). Net: same as above plus a discarded diff.

To make a subtask finishable, every dispatch must carry:

1. **Hard scope boundary.** Files allowed to touch (or files explicitly off-limits). "Edit only `apps/backend/src/users/user.entity.ts` and the matching migration." Never "do the backend changes" — that's a research task disguised as a work task.
2. **Acceptance criteria as a check the agent runs itself.** A green command — `npx prisma validate`, `pnpm test users.spec.ts`, `tsc --noEmit`, an `eslint` pass on a specific file. The agent is told to keep iterating until that command exits 0 and to surface its output in the final answer. This replaces coach-side review for ~80% of the work.
3. **Done definition in one sentence**, written so the coach can confirm it in seconds without reading the diff line by line. "Column added, migration generated, prisma validate green." If the coach can't write such a sentence, the subtask isn't sized right yet — split or specify further before dispatching.
4. **Context the agent needs but doesn't have.** Spec section excerpts, naming conventions, the one related file it should mirror. Paste these into the prompt; don't make the agent find them by reading half the repo. If your team keeps durable notes (a docs folder, a wiki, a notes vault), pass the *exact* paths worth reading rather than asking the agent to go looking.
5. **Match to capability.** Codex / agy-Gemini do well on pattern-following, well-scoped CRUD, test scaffolding, mechanical refactors, and writing within an existing convention. They do poorly on architectural decisions across files they haven't been pointed at, novel API design, and judging whether a spec rule applies. Keep those for the coach — or for agy's `claude-opus-4-6-thinking` when the subtask genuinely needs top-tier judgment but should not burn the Anthropic Opus weekly. If a profile in `.playmaker/agents/<name>.md` exists, trust its guidance over these defaults.

A useful smell test before dispatching: *"If this came back done, would I review by running one command and reading one paragraph — or would I need to read the whole diff and think hard about whether it's right?"* If the latter, re-scope before sending.

Output the plan as a short proposal:
```
Plan:
- Coach (me): schema design + integration glue
- Codex: FastAPI handlers in apps/api/
- agy (gemini-3.6-flash-high): pytest tests + README

Quotas: Claude session 93% / weekly 80%, Codex 100%,
        agy Gemini 5h 100% / weekly 96%, Claude/GPT 5h 88% / weekly 71%.
OK to proceed?
```

### 4. Wait for explicit approval

Do not dispatch anything until the user confirms. Approval can be selective:

- "go" / "ok" → execute as proposed
- "go but skip agy" → drop agy's tasks, redistribute
- "go but reroute tests to claude" → swap agent for that subtask
- "no, change X" → revise and re-propose

If the user modifies the plan, re-state the modified plan in one line, then dispatch.

### 5. Dispatch in parallel

For each delegated subtask:

```bash
playmaker dispatch <agent> --model <name> --prompt "<scoped prompt>" --cwd $(pwd)
```

`playmaker dispatch` is **detached by default** — it returns immediately with a session id and the agent runs in the background. That's the whole point of the coach pattern; never wait on a single agent unless you specifically need its output before doing anything else.

Always pass `--cwd $(pwd)` — `playmaker`'s default is the *coach process's* current dir, which is not always what you want.

Always pass `--model` when you've made a tier-matching decision in step 3. Without it, the agent CLI uses its own default (which may be its top-tier model, defeating the load-distribution effort). Model name is what the agent's native CLI accepts: `claude --model sonnet`, `agy --model gemini-3.6-flash-high` — for agy, copy the line from `agy models` rather than typing it.

Two exceptions to "always pass `--model`":
- **codex** — its model roster depends on the account plan, and an unavailable name fails the whole dispatch (`codex turn failed: … not supported when using Codex with a ChatGPT account`). Omitting `--model` uses whatever that account actually has, which is usually what you want.
- **agy** — its own default is a top-tier model, so omitting `--model` on a dispatch meant to be cheap silently spends the expensive bucket. Always pass it here.

**opencode is `provider/model`, and the default is invisible.** Names look like `zai-coding-plan/glm-5.3` or `lmstudio/qwen/qwen3-coder-30b` — **run `opencode models` and copy a line** rather than writing one from memory; playmaker validates against that roster and fails the dispatch on a name it doesn't contain. Omitting `--model` falls through to opencode's own default, which is whatever the user last picked interactively — and it is *not* necessarily written to `~/.config/opencode/opencode.json`, so you cannot read it back from a config file. The only way to know what an unqualified dispatch will run is to look at the `providerID`/`modelID` on a past session in `opencode.db`. So for opencode, always pass `--model` unless `[agents.opencode] model` is set in `~/.playmaker/config.toml` — which is the cheap fix: pin it there once and the lane becomes deterministic.

**agy prompt discipline:** the agy agent's shell lives in a private scratch directory, not the workspace. playmaker automatically prepends a workspace preamble to every agy dispatch, but reinforce it: word file instructions with paths relative to the workspace root or absolute paths, never "in the current directory".

**Bad-model handling (playmaker ≥0.4 does this for you):** a wrong `--model` is the classic silent failure. Codex returns a `turn.failed` while exiting 0 with empty output; agy silently runs its *default* model instead of erroring. playmaker catches both — a codex model/auth failure raises `codex turn failed: …`, and an unknown agy model raises with the valid roster before dispatch. So a dispatch that comes back **failed** with a model message means: fix the `--model` string (for agy, copy it exactly from `agy models`) and re-dispatch. Don't retry the same string.

**For sibling Claude, `--model sonnet` is the default, `--model haiku` for trivial mechanical work.** External `claude -p` draws on the subscription like everything else; defaulting to Sonnet keeps the work on Sonnet's separate weekly bucket and spares the scarce Opus one, so Opus externally must be a deliberate choice. Claude-side writes work on either lane — internal sub-agent when the coach folds the result in directly, external dispatch when you want a tracked, detached stream.

**Tag every fan-out with `--batch <label>`.** Pass the *same* short label to all dispatches you launch as one parallel batch. playmaker then suppresses per-agent success pings and fires a **single "N/N done" summary** when the whole batch drains — the signal the user actually waits on (its notification is clickable and opens a combined view of all outputs). Failures still ping immediately. Omit `--batch` only for a true one-off dispatch.

```bash
B=admin-dashboard   # any short label shared across this fan-out
playmaker dispatch codex --batch "$B" --prompt "..." --cwd $(pwd)
playmaker dispatch agy   --batch "$B" --model gemini-3.6-flash-high --prompt "..." --cwd $(pwd)
```

`playmaker continue <id> --model <name>` overrides the model for one follow-up turn while keeping the live session; without `--model` it inherits the parent session's model.

For sequential delegation (e.g. "I'll do schema first, then Codex builds on top"), commit a checkpoint to git and use `--sync` to block:

```bash
git commit -am "checkpoint: schema before backend dispatch"
playmaker dispatch codex --prompt "..." --cwd $(pwd) --sync   # blocks, prints output
```

### 6. Coach's own work

Between polls, do the part of the task you reserved for yourself in this thread.

### 7. Monitor

Periodically (or after finishing your own chunks):

```bash
playmaker list                         # who's done, who's running
playmaker get <id> --wait              # block until specific agent finishes
```

### 8. Review

For each finished subtask:

```bash
playmaker summary <id>                 # the agent's own narrative of what they did
git diff <since>                       # what they actually changed in code
```

Read `summary` first. If you need more context to judge: `playmaker thread <id>` (last 5 turns by default). If actively debugging *why* an agent went sideways: `playmaker thread <id> --all --include-tools`. The default `--max-bytes 50000` is a safety cap, not an excuse to skip selectivity.

### 9. Decide per subtask

- **Approve**: nothing more to do; move on
- **Resume for follow-ups (default)**: small fix or refinement →
  `playmaker continue <id> --prompt "Y is still broken because Z — please fix"`.
  This sends the prompt into the agent's live session, so its prior reasoning,
  tool history, and file context are still in scope. Don't re-paste context
  the agent already has.
- **Fresh dispatch only when the old context is a liability**: requirements
  shifted materially, the agent is looping/confused, or you hit tool errors
  that won't resolve mid-thread. Then start clean and link the lineage:
  `playmaker dispatch <agent> --prompt "..." --parent <id>`.
- **Fix it yourself**: small enough that re-dispatching is overkill
- **Hand to user**: bigger redesign needed; surface the issue

### 10. Failure handling

If `playmaker dispatch` returns an error (binary missing, auth bad, agent unavailable), don't silently retry. Surface it, propose a re-routed plan ("Codex unavailable; want me to give the backend to Claude instead?"), and wait for user confirmation.

**Sibling-Claude writes are enabled, but bounded.** playmaker dispatches `claude -p` with `--permission-mode acceptEdits` by default: it edits files and runs commands inside the dispatch `--cwd` without asking, and claude itself refuses anything outside that directory. So **keep every path in the prompt inside `--cwd`** — a subtask that legitimately needs to touch a sibling repo or a dotfile in `$HOME` will come back refused, and the fix is a different `--cwd` (or the user's `yolo = true`), not a re-prompt.

Both Claude lanes are on the subscription, so pick by **where the work lives**, not cost:
- **Coach folds the result in directly → internal sub-agent (Task tool).** In-session, write-capable, returns into context. Default choice for "more Claude."
- **Independent stream you'll monitor / continue separately → `playmaker dispatch claude --model sonnet`.** Tracked, detached; Sonnet's separate weekly bucket spares Opus.
- **Work that can leave the Claude family → Codex / agy / opencode** (their own quotas, or none at all for a local model).

If a dispatch comes back with zero file changes, check `playmaker summary <id>`. Two common causes for Claude: the subtask tried to write outside `--cwd` and was refused (re-dispatch with the right `--cwd`), or the run needed a permission the configured mode doesn't grant — a "I need your permission" answer, not a crash. For **agy** specifically, a "done" with no file changes usually means the files landed in agy's private scratch dir (`~/.gemini/antigravity-cli/scratch/`) — the prompt referred to "the current directory" instead of workspace paths; re-dispatch with explicit paths.

## Reading discipline

- `playmaker summary` first — usually answers "is it done and what did it claim to do"
- `playmaker thread <id>` (default last 5) — when summary is insufficient
- `--include-tools` and `--all` — only when actively debugging agent logic
- Watch out for context bloat: a long agent thread can be tens of thousands of tokens. The `--max-bytes` cap protects you, but you should pre-decide what you need before reading.

## Quota awareness

- **Read Claude quota at model granularity:** Sonnet weekly and Opus weekly are **independent buckets**. Coach and internal sub-agents on Opus spend the scarce one; Sonnet usually sits idle. Push mid-tier work to Sonnet (an internal sub-agent set to Sonnet, or `dispatch claude --model sonnet`) to spare Opus.
- Read quotas at *model* granularity, not *agent* granularity. For agy, the probe reports four windows — `Gemini 5h/weekly` and `Claude/GPT 5h/weekly`; for Codex, top-tier vs lighter modes; for opencode, the quota is reported under the **provider** it is pointed at (`Z.ai`), and a local model reports nothing because it spends nothing.
- Skip a *model* if its `*_left` is below ~10%; reroute the subtask to the same agent with a different model, or to a different agent entirely.
- If a top-tier `weekly_*_left` is degrading toward the deadline, push as much work as possible to mid- and cheap-tier models on the same provider (which are often nowhere near depleted), instead of switching providers blindly.
- agy's **5-hour** window is the one that bites during a fan-out: it "smooths aggregate demand", so a burst of agy dispatches drains `Gemini 5h` or `Claude/GPT 5h` well before the weekly. If a 5h window is low, spread the burst or move some slices to Codex/coach.
- If `quotas.json` is stale (>1h), refresh before drafting the plan; if you can't refresh, mention it in the plan.

## Tooling reference

```
playmaker agents                                # who's installed
playmaker quotas [--refresh]                    # capacity, broken out per model
playmaker dispatch <agent> [--model NAME] [--batch LABEL] --prompt "..." [--cwd <dir>] [--files ...] [--sync]
playmaker continue <id> [--model NAME] --prompt "..."   # resume sub-agent in its existing session
playmaker list [--status running|done|failed] [--agent NAME] [--limit N]
playmaker get <id> [--wait]                     # metadata + final output
playmaker summary <id>                          # last 2 assistant messages
playmaker thread <id> [--last N] [--all] [--role assistant|user|tool]
                     [--include-tools] [--max-bytes N] [--follow]
playmaker logs <id> [--follow]                  # subprocess stdout for detached runs
playmaker kill <id>                             # SIGTERM
playmaker watch                                 # Rich live TUI of sessions
```

Every command takes `--json` for machine-readable output.

`--model NAME` is forwarded to the agent's native CLI: `claude --model sonnet`, `agy --model claude-opus-4-6-thinking`, `codex -m <whatever that account has>`, `opencode -m zai-coding-plan/glm-5.3`. Without it the agent CLI uses its own default. Model is stored on the session row, so detached re-runs and `continue` inherit it; `continue --model X` overrides for that one turn.

**The agy roster is not documented here on purpose** — it changes with Antigravity releases, and so does the spelling convention. Run `agy models` and copy a line. At the time of writing it returns bare slugs in the shape `gemini-3.6-flash-{low,medium,high}`, `gemini-3.1-pro-{low,high}`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking`, `gpt-oss-120b-medium` — but treat that as an example of the *shape*, not a list to copy from.

## Anti-patterns

- **Coach doing everything itself "to be safe".** Defeats the point. If the task fits the activation criteria, *delegate*.
- **Coach doing its own codebase recon when a Flash/Sonnet recon dispatch would do it cheaper.** Reading is the cheapest model's job; coach reads the *summary*, not the codebase. See step 1a.
- **Coach dispatching for trivial tasks.** Activation overhead. Just do them.
- **Coach dispatching subtasks that are too big or too vague to verify cheaply.** If reviewing the result requires reading the whole diff and judging architectural decisions, the coach pays in top-tier tokens what it tried to save. Re-scope: tighten file boundaries, attach a self-running check, write a one-sentence done-condition. Re-prompting an agent that's gone sideways is the most expensive way to use this tool.
- **Coach delegating tasks above the agent's ceiling.** Architectural judgment, cross-module integration, spec interpretation belong to the coach (or agy-Opus as the top-tier overflow lane). Codex/agy-Gemini are best on pattern-following inside a tight scope.
- **Coach ignoring per-model quotas.** Burning top-tier weekly to dispatch ten things in series when Sonnet/Flash were sitting idle is the original problem this skill was built to solve. Top-tier work goes to top-tier models *only*. Pattern-following work goes to mid-tier. Recon goes to cheap-tier.
- **Coach defaulting to top-tier for every dispatch by omitting `--model`.** Without an explicit `--model`, the agent CLI tends to use its top-tier default and depletes the wrong bucket.
- **Coach silently retrying or hiding agent failures.** User stays informed even when things go wrong.
- **Coach treating `playmaker dispatch` as the only delegation lane.** In-session `Task` sub-agents are a first-class lane — in-session, write-capable, parallel. When you want "more Claude," reach for an internal sub-agent before an external `claude -p`. Don't be shy about spawning several.
- **Coach burning the Opus weekly bucket when Sonnet was idle.** Dispatching Opus via `claude -p`, or doing mid-tier work on the coach itself, depletes the scarcest bucket for something Sonnet — a separate, usually-idle weekly bucket, via an internal sub-agent or external dispatch — would have done. Default sibling Claude to Sonnet; reserve Opus for genuine top-tier judgment.
- **Coach reading whole agent threads into context unprompted.** Use `summary` and selective `--last N`.
