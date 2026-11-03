---
name: playmaker-coach
description: Team-lead mode for coding work. Decompose the task into work packages, dispatch them to Claude/Codex/Antigravity(agy)/opencode/Kimi Code(kimi)/Muse Code(muse)/Manus (manus)/Perplexity (perplexity) workers through the `playmaker` CLI, then run an automatic multi-agent review board over every diff before it lands, and drive the fix cycles. Use for ANY request that will change code in more than one place, needs an independent review pass, or has two or more parts that can run at once — "implement", "add", "fix", "refactor", "wire up", "сделай", "почини", "добавь", "реализуй", "собери". NOT for answering a question, reading or explaining code, a single-line edit, or a git/ops command.
---

# playmaker-coach — you are the tech lead, not the typist

`playmaker` dispatches sub-tasks to Codex / Antigravity (`agy`) / opencode / Kimi Code (`kimi`) / Muse Code (`muse`) / Manus (`manus`) / Perplexity (`perplexity`) / a sibling Claude,
tracks them, and returns their threads. This skill is the judgment on top: what to split,
who gets which slice, how to size it so verifying is cheap, and **how the review board runs**.

**The coach produces plans, prompts, verdicts and integration — not feature diffs.** Your context
window is the most expensive resource on the table. Every file you read yourself and every line you
type yourself is one a cheaper worker could have produced. Push out implementation, recon,
summarization, and even the drafting of worker prompts when the task is big enough.

Two loops run under your hand, always both:

```
decompose → dispatch → prove on disk → REVIEW BOARD → adjudicate → continue
                                            ↑______________________|   max 2 cycles; third → escalation seat
all WPs green → integrate → GLOBAL GATE → SEAM REVIEW (≥2 interacting WPs) → land → ledger row
```

## 1. Activation gate

Run this gate on every request that touches code. It has three questions:

1. **Will this change code in more than one file, or in one file in a way that deserves a second pair of eyes?**
2. **Are there ≥2 slices that could run at the same time** (backend + frontend, code + tests, two modules)?
3. **Is there a durable artefact** (a diff someone will have to live with), as opposed to an answer?

**Any "yes" → activate.** The default is delegation; doing it yourself is the exception you justify.

**Skip the skill** only for: a question, an explanation, reading/searching code, a one-line or
one-symbol edit, a git/ops command, or a task the user explicitly asked you to do by hand
("сам", "solo", "не делегируй", "just do it yourself").

Borderline single-WP tasks still activate — with **one** worker and the review board. The board is
the point: a single reviewed WP is a legitimate, common shape, not overhead.

## 2. Load policy before planning

Defaults in this file are the *mechanism*. The *policy* — which quota to spare, which lanes are
contended, what this repo forbids juniors to touch, which commands are the objective gates — lives
outside the skill so it survives `playmaker skill install --force`. Read, in this order, and let
later files override earlier ones:

```bash
cat ~/.playmaker/policy.md      2>/dev/null   # personal policy (tiers, quota economics, lane defaults)
cat ./.playmaker/policy.md      2>/dev/null   # repo policy (gates, protected paths, conventions) — wins
ls  ./.playmaker/agents/*.md    2>/dev/null || ls ~/.playmaker/agents/*.md 2>/dev/null
```

Agent profiles describe each lane's traps, mechanics and quota position — trust a profile over the
generic defaults here, never over the policy's tier table or a dated owner order: profiles say how a
lane behaves, policy says what it may be given. A newer dated order beats an older line anywhere. If no policy file exists, say so once in the plan and use these defaults.

## 3. Protocol

### 3.1 Recon — delegate it

Codebase exploration is the highest-leverage thing to delegate. Two kinds, two tiers:

- **Map recon** — locate files, symbols, call sites, line ranges, inventories. Raw reading: the
  cheapest lane (Gemini Flash) does it as well as you do.
- **Semantic recon** — invariants, ownership, contracts, side effects, impact radius, «why is it like
  this». Judgment: a senior lane (Kimi K3 for many-file sweeps, Gemini Pro, Codex). A cheap model
  returns a confident wrong map, and you plan on it.

Before your own `grep`/`Read` sweep, dispatch the matching kind with an explicit deliverable and
`--sync`, and read a 200-word report instead of ten files:

```bash
playmaker dispatch agy --model <cheap-tier> --cwd "$(pwd)" --sync --read-only \
  --prompt "Recon only — change nothing. Locate (a) …, (b) …, (c) …. Report under 200 words as a numbered list with file paths and line ranges."
```

Skip it for one-file, one-symbol lookups — just `Grep`.

Web lookups the coach would run itself — a version, flag, price, or API change — go to `playmaker dispatch perplexity --sync --read-only --model low`; rules are in `references/lanes.md`.

### 3.2 Quotas

```bash
playmaker quotas               # capacity per MODEL, not per agent; re-probes itself when stale
```

Read at model granularity: separate buckets inside one provider are separate capacity. The aim is
**accepted work per point of scarce quota**, with prepaid capacity spent before it resets.
Level-loading — every pool drawn down evenly by week's end, except the coach's own — is the
tie-breaker between lanes that fit a WP equally, never a reason to queue a WP behind a closed lane. Details and per-provider quirks:
`references/quotas.md`.

### 3.3 Decompose into work packages

2–5 WPs on the first pass. Name each WP's **class** first — `mechanical | feature | terminal-heavy |
repo-recon | architecture | high-risk` — it decides which senior lanes are eligible (policy: fit before
headroom), which lenses the board gets, and it is the first field of the ledger row. Then a WP is
dispatchable only when all five hold — this is what makes review cheap and re-prompting rare:

1. **Hard file boundary.** "Edit only `x.ts` and its spec" — never "do the backend part".
2. **A gate the worker runs itself** — `tsc --noEmit`, a named spec file, a lint pass. It must exit 0
   before the worker reports done, and its output goes in the final answer.
3. **A one-sentence done-condition** you can confirm in seconds. Can't write it? The WP isn't sized
   yet — split or specify first.
4. **The context it lacks** pasted in: the spec excerpt, the neighbouring file to mirror, exact paths
   of any notes worth reading.
5. **Match to the lane's ceiling.** Pattern-following inside a tight scope → juniors. Architectural
   judgment, cross-module integration, spec interpretation → senior lane or you.

Smell test before dispatching: *"if this comes back done, do I verify it by running one command and
reading one paragraph — or by reading the whole diff and thinking hard?"* If the latter, re-scope.

Lane and tier selection: `references/lanes.md`. Risk classes and what juniors may never touch: repo
policy first, then `references/lanes.md`.

### 3.4 Propose once, then run

Post the plan as: WP → class → lane+model → why → gate → done-condition, plus the reviewer pair per WP and
the current per-model capacity. The plan is approved once per task; after that, dispatch further
waves, fix cycles, re-boards and integration without asking again. Approval may be partial ("go but
reroute tests"); restate the modified plan in one line, then dispatch. Owner decisions never stop the
conveyor: see §3.11.

### 3.5 Dispatch

```bash
B=<short-batch-label>
playmaker dispatch <agent> --model <name> --batch "$B" --cwd "$(pwd)" --worktree <wp-name> \
  --gate "<cmd>" --fallback <lane,lane> --prompt "<WP prompt>"
```

Detached by default — that is the point; never `--sync` a whole fan-out. Always pass `--cwd`,
`--batch` (one summary ping for the batch), and `--model` unless the profile says otherwise.
Prompt shape: `references/prompt-templates.md`. Per-agent traps (agy scratch dir, opencode relative
paths, codex model roster, kimi's K2.7 default and exit codes, muse's sandbox): `references/agent-gotchas.md`.

Parallel WPs are isolated by the CLI: `--worktree NAME` creates `<repo>-wt/NAME` on branch
`wp/NAME` and runs the repo's `.playmaker/wt-setup.sh` or `scripts/playmaker/wt-setup.sh` hook when
present. A failing hook aborts dispatch before any quota is spent. The gate travels with dispatch as
`--gate "<cmd>"`; the CLI runs it in the worktree after the agent exits and records `gate_status`
(`pass`, `fail`, or `timeout`). `--fallback lane,lane` reroutes a quota or auth refusal itself.

The wake signal is `playmaker events --follow --batch "$B"` (JSON lines; run it under Monitor).
For a dispatch made without `--fallback`, keep the manual one-minute `playmaker get <id> --json`
probe: a quota or auth refusal finishes in seconds as `failed`, pings nobody, and looks exactly like
a slow worker. Give every dispatch a deadline (recon 15 minutes; a WP by its size) and on expiry
check the process is alive (`playmaker logs <id>`, its RSS) before waiting longer. Never wait for a
closed lane. At landing run `playmaker worktree rm NAME`; after a fan-out run `playmaker worktree
prune-done`. The team once accumulated 37 worktrees (2026-10-13, ≈30 GB with full `node_modules`); cleanup is part of landing.


### 3.6 Prove it on disk before you believe it

`done` means "the process exited cleanly with text", not "code changed". playmaker ≥0.9 marks a
zero-change write task `no_changes` — treat it exactly as a failure. On older builds, check yourself:

```bash
git -C "<cwd>" status --short --untracked-files=all   # empty tree on a write WP = NOT done
```

New files are invisible to `git diff`: `git add -N <new files>` before the board, and compare
`git diff --stat` with what the worker claims it changed.

Read `gate_status` from `playmaker get <id>`. Re-run the WP's own gate by hand only when the
dispatch carried no `--gate`. A WP that fails its gate never reaches the review board — it goes
straight back to the worker.

### 3.7 Review board — the mandatory second loop

**Every WP that changes code gets reviewed by agents, not by you reading the diff.** You read
verdicts, not code. Composition, prompts, the verdict contract and the cycle rules are in
`references/review-board.md`; the fan-out itself is one command:

```bash
pm-review <wp-label> <base-ref> [--risk routine|normal|high] --gate "<cmd>" --impl-agent <lane>
pm-review --collect <wp-label>          # once the review batch drains
```

(`pm-review` is `scripts/review-board.sh` from this skill; without the shortcut on `PATH`, call
`~/.claude/skills/playmaker-coach/scripts/review-board.sh` directly.)

### 3.7.1 Decision review — advisory senior lanes for a disputed artefact

Convene `pm-review-decision <slug> --question <file>` before dispatching a WP spec of class
`architecture` or risk `high`, when two reviewers disagree about one hunk, when you mark a choice
`спорно`, or when the owner asks. It fans the first three senior `decision` roster lanes (and an
Opus fourth seat only with `--opus`) across one artefact with the question «what breaks», then
mechanically records agreement or split; use at most two per day without the owner's word. It is
read-only and never stops the cores of a wave. On a split, decide yourself or bring the owner one
bounded question—not the raw disagreement. Contract, budget, and flags: `references/review-decision.md`.

Every board run starts with a **precheck**: before any reviewer is dispatched, a cheap lane from
the roster's `precheck` block checks the spec's acceptance list against the diff and answers in
about half a minute. A fail — an acceptance item with no evidence in the diff — blocks the board
with a ready numbered fix list that goes straight back to the worker via `playmaker continue`,
exactly like a board verdict; a pass dispatches the board; and when no cheap lane answers the
board runs anyway, because the precheck must never block the pipeline by being down. `--no-precheck`
skips it, `--precheck-only` runs it alone (details: `references/review-board.md`, «Precheck»).

**The default way to run a board is the automatic loop.** One command drives the whole cycle
without you — wait the implementer and its CLI gate → precheck → wip commit → fan-out → wait the
seats (one substitution per dead seat) → collect → mechanical adjudication → fix list back via
`playmaker continue --after` → delta round — to green or a stop:

```bash
pm-review --auto <wp-label> <base-ref> --impl-id <impl-session> --paths "<wp pathspecs>" --gate "<cmd>" --impl-agent <lane>
```

It runs in the foreground, so background it and read the final summary it prints (every step is
in `.playmaker/reviews/<wp>/auto.log`): exit 0 = green, 3 = stopped with blocking findings left
or an incomplete board — escalate per policy, 4 = the implementer failed — reroute. It writes a
`wip: <wp> rN (auto)` commit per round so round N+1 reviews the delta only; squash before
landing. Your part is what the loop refuses to do: arbitrate disagreements, rule on the spec,
own the escalation seat (details and flags: `references/review-board.md`, «Automatic loop»).

Non-negotiables:

- **The implementer never reviews its own WP**, and reviewers do not see each other's verdicts.
- Reviewers are **`--read-only`** and prompted to **refute** the WP against its acceptance criteria,
  not to summarize it.
- Every finding carries `file:line` + a concrete failure scenario. **No evidence → dropped.** You
  arbitrate, and a reviewer's confidence is an input, not a verdict.
- Fixes go back via `playmaker continue <impl-id>` as a numbered list — the worker still has its
  context. Re-review runs on the **delta only** — which means passing the round-1 state as the
  base-ref (a wip commit in the worktree, squashed before landing); with the original base the
  patch is cumulative and reviewers re-open settled code.
- A ruling that changes the scope goes into `spec.md` before the next round — reviewers refute
  against the spec, and a stale spec produces false blockers. Before charging a finding to the
  worker, check it is not already present on the base ref.
- **Two cycles maximum on the implementing lane.** Still blocking after two → send the third cycle
  to the escalation seat named by policy (Codex Sol), in a fresh prompt containing both verdicts and
  the fix list. Tell the user in the board summary; ask the user only for OWNER decisions: product
  behaviour, money, design, production config, or legal. Owner order 2026-10-07: «Все пакеты
  доделать надо» — waves run to the end without protocol questions.
- Land only at **zero blocking findings**.
- On `high`, the `risk` seat is told to **falsify** the acceptance criteria — a command, input or repro
  per criterion it attacked, not only an argument; its `pass` lists what it tried. A seat that cannot
  execute (the `claude` lane runs with binaries forbidden) never holds `correctness` — give it `contracts`.

### 3.8 Keep a board file

Long fan-outs outlive your context. Maintain `./.playmaker/board.md` — one row per WP:

```
| WP | lane/model | impl id | gate | reviewers (ids) | verdict | cycle | state |
```

Keep a second fixed table for owner decisions:

```
| D | question (one choice: A/B or yes/no) | asked at | status (open / answered) | answer | blocks (WP/tail) |
```

There is no `default` status and no deadline column by design.

Update it at dispatch, at gate, after each review round. On resume, read the board before anything
else. It is also what you paste back to the user as the status report.

The columns are fixed — one layout, not one per board: a resumed session and the ledger both parse
them. At landing, the ledger hook writes the WP's row to the cross-repo ledger on `git commit`; read its
`[ledger]` line and correct the soft fields with `python3 ~/.playmaker/scripts/ledger.py fix wp=… k=v`.
When the hook matched nothing, append the row by hand: `python3 ~/.playmaker/scripts/ledger.py add
wp=… class=… impl_lane=… …` (fields: `~/.playmaker/policy.md`, «Ledger»). It keeps what board prose loses: class, first-gate pass, blocking findings that survived
adjudication, cycles, wall time — the evidence the routing step reads.

### 3.9 Failures

Surface them; never silently retry — and never wait. `--fallback` covers quota, auth, and binary
refusals; reroute by hand only when the dispatch had no fallback list. A failed dispatch is neither a
fix cycle nor a worker failure: re-route the WP to the next live senior lane now, with the full
context in a fresh prompt, and record the substitution in the board. Ask the user only when no
eligible lane is left. Diagnosis per agent: `references/agent-gotchas.md`.

### 3.10 Integrate before landing — when the fan-out had two or more WPs

Two green WPs can be wrong together. Per-WP gates and boards prove the parts; nothing above proves the
whole. Once every WP is green:

```
integrate on one tree (worktrees merged in dependency order)
  → GLOBAL GATE — the repo's minimum bar plus the union of the WPs' own gates, run by you
  → if ≥2 WPs touched interacting modules:
      SEAM REVIEW — pm-review <batch>-seams <base-before-first-WP> --risk seams --impl-agent <main lane>
  → commit, one WP per commit, in integration order → ledger rows
  → playmaker worktree prune-done
```

"Interacting" = shared types or packages, the same service or screen, a migration and its consumers,
event or config names, shared state. The seam reviewer gets a spec that lists the seams and reads only
those: changed interfaces, duplicated or conflicting assumptions, incompatible types, migration order,
naming, config, shared state. The packages' internals already passed their own boards — re-reviewing
them is the third round the protocol forbids. Roster: the `seams` block in `reviewers.conf`;
`--impl-agent` is the lane that implemented most of the fan-out.

### 3.11 Decisions in flight

A WP that depends on an owner decision is split into a core that needs no answer and a tail that
does. Dispatch the core now. Ask the owner the moment the question is found: one choice, A/B or
yes/no, with no default and no deadline. While the owner is silent, everything that needs no answer
proceeds — other WPs, other tails, the next wave, the boards; only that tail waits. Silence is never
a default; the coach never decides an owner question by timer.

Deliver the answer with `playmaker continue <impl-id> --after <impl-id> --prompt "<tail>"`: it is
queued and delivered when the worker is `done`; a `continue` on a live session is lost. When the
worker is gone, dispatch the tail as a separate mini-WP on another lane. Put numbers and thresholds
behind the repo's config keys where it has them, so code lands and the owner sets the value.

Owner words (2026-10-18): «начать делать то, что понятно, а по решениям выяснять и досылать воркеру»;
«молчит не равно дефолт! молчит, значит делает всё, что не требует ответа».

### 3.12 No synchronous waiting

Never block on one process or one board. Keep a ready queue of WPs with prompts written, so a freed
lane takes the next one at once; dispatch wave N+1 while wave N sits on the board. Wake on
`events --follow` under Monitor, never a sleep loop. Start the longest WP first: it is the critical
path. Cap parallelism with `[limits] max_parallel` in `config.toml`; RAM, not quota, is the ceiling.
Owner words (2026-10-18): «Конвейер внахлёст. Максимально уменьшить простой из-за ожидания».

## 4. What the coach may still type by hand

Allowed: integration glue between WPs, conflict resolution, config/one-liners smaller than the
prompt that would describe them, and the final commit. Everything else — including "it's faster if
I just do it" — is the anti-pattern this skill exists to kill. If you catch yourself opening an
editor on product code, ask whether that is a WP you failed to write.

## 5. Reference index

| File | Read it when |
|---|---|
| `references/lanes.md` | choosing an agent/model, junior-vs-senior routing, escalation |
| `references/review-board.md` | any review round — composition, prompts, verdict contract, cycles |
| `references/prompt-templates.md` | writing a WP, reviewer, or follow-up prompt |
| `references/quotas.md` | reading `playmaker quotas`, per-provider bucket structure |
| `references/agent-gotchas.md` | a dispatch behaved strangely, or before a first dispatch to a lane |
| `references/commands.md` | exact CLI surface and flags |

## 6. Anti-patterns

- **Doing the work "because it's faster".** It isn't, once review is counted — and it burns the
  scarcest bucket in the room.
- **Skipping the review board on a small WP.** Small WPs are where unreviewed bugs hide; the board
  costs one command.
- **Reading diffs instead of verdicts.** Reviewers exist to keep the diff out of your context.
- **Reviewing with the implementer's own lane**, or letting reviewers see each other's output.
- **Trusting `done`** without a disk check and a gate.
- **A third fix cycle on the same lane.** Send the third cycle to the escalation seat instead.
- **Waiting synchronously for a worker or a board.**
- **Defaulting an owner decision by timer or by silence.**
- **Trusting a gate the CLI did not run.**
- **Dispatching a WP you cannot verify in one command and one paragraph.**
- **Omitting `--model`** and letting a CLI default drain a top-tier bucket.
- **Reading whole agent threads** when `summary` answers the question.
- **Landing two green WPs without running the tree they make together.** Per-WP gates prove the
  parts; the global gate and the seam review prove the whole.
- **Routing by headroom alone.** Class first, then the ledger's floor, then the pool floors, then headroom.
