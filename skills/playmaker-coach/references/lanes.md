> **Opus 5 lane — OUT of the review board (owner order 2026-09-08):** `playmaker dispatch claude -m opus` draws the Anthropic all-models weekly — the bucket the coach session falls back to (as Opus) when the Fable weekly runs out — and was eating it fast. It no longer holds the `high` risk seat by default (that was the 2026-09-02 order); `codex` does. Opus is weighted by the Fable weekly instead: while `Weekly · Fable` ≥ 50% it fills the risk seat on `high` boards that Codex implemented, ¼ of that at 25–50%, nothing below 25% or when the all-models weekly is under 50%. Never for implementation or recon, never on `normal`. Table and flip: `~/.playmaker/policy.md`, `~/.playmaker/reviewers.conf`.

> **GEMINI ONLY on agy (owner order 2026-09-02):** never dispatch `claude-*` or `gpt-oss-*` models on the `agy` lane. Want a Claude? Don't dispatch one: the coach session is the Claude, and the Anthropic bucket is out of the board (2026-09-08). See `~/.playmaker/policy.md`.

# Execution lanes — where work runs

All Claude work — coach, in-session sub-agents, external `claude -p` — draws from the **same Claude
subscription**. So routing is about *which bucket* you spend and *where the result lands*, not who
pays. Four lanes:

1. **Coach (this thread).** Interactive Claude Code on the top-tier weekly bucket — the scarcest,
   hardest to replenish. Serial, and the most expensive in *context*. Reserve for orchestration,
   architecture judgment, adjudication, final integration.

2. **In-session sub-agents (the Task/Agent tool).** Run inside this session, write files (they
   inherit the coach's permission mode), return straight into the coach's context, run in parallel.
   Same subscription. Use when the coach folds the result in directly.

3. **External dispatch — `playmaker dispatch claude`.** A tracked, detached stream you can monitor
   and `continue` independently. Same subscription, and since September 2026 the same bucket: Sonnet has no
   separate weekly (`seven_day_sonnet: null`), so this lane spends the coach's own all-models weekly.
   Default `--model sonnet` (`haiku` for trivial mechanical work), and only when policy allows it. playmaker runs it in `acceptEdits`: it writes freely
   inside `--cwd` and is refused outside it, so keep every path in the prompt inside `--cwd`.

4. **External dispatch — `codex` / `agy` / `opencode` / `kimi` / `muse`.** Each on its own subscription or plan — the
   home for write-heavy parallel implementation that can leave the Anthropic subscription.
   - **`agy` (Antigravity)** carries more than Google models: alongside Gemini Flash and Pro tiers it
     serves **Claude Sonnet/Opus (Thinking)** and a GPT-OSS tier. Its Claude runs on *Google's* pool —
     capable judgment that spends none of the Anthropic bucket, but a generation behind the
     frontier: treat the whole lane as **middle** (see Model tiers below). Note the internal
     split: all Gemini models share one bucket, Claude and GPT-OSS share another.
   - **`opencode`** is the widest lane: one CLI over ~75 providers addressed as `provider/model` — a
     GLM coding plan, or a model running locally on this machine, which spends no subscription quota
     at all.
   - **`kimi`** runs the Kimi Code CLI on its own subscription: senior tier (K3), native login, not
     via opencode.
   - **`muse`** runs Meta's Muse Code CLI on its own login: senior tier (Muse Spark), sandboxed
     by default.

**Never write an agy or opencode model name from memory** — run `agy models` / `opencode models` and
copy a line. Both rosters and their spelling move with releases, and playmaker validates `--model`
against the live roster, failing the dispatch on a stale name.

## Routing cheat-sheet

| The work… | Lane | Why |
|---|---|---|
| writes files, coach integrates the result directly | in-session sub-agent | write-capable, returns into context |
| is an independent stream to monitor separately | `dispatch claude --model sonnet` | tracked, detached — but on the coach's own subscription (Sonnet has no separate bucket): policy says when, and it is rarely |
| is heavy reasoning only the coach can do | coach | top tier, serial |
| is write-heavy and can leave Claude | codex / agy / opencode / kimi / muse | their own quotas |
| needs senior judgment on a pool nobody else on the machine draws from | `dispatch kimi -m kimi-code/k3-256k` / `dispatch muse` | K3 and Muse Spark on their own subscriptions; K3 is slow, so detached only |
| needs a second strong reviewer without touching the Anthropic bucket | `dispatch agy --model <gemini-pro-high>` / `dispatch muse` | near-senior judgment on an uncontended pool; Muse Spark is senior |
| wants Claude-flavoured judgment without spending the Anthropic bucket | `dispatch agy --model <claude-opus-thinking>` | middle tier — a generation behind; never the only senior eye |
| is bulk work with every subscription low | `dispatch opencode --model <plan>/<model>` | a separate plan, untouched by the others |
| is mechanical and privacy-sensitive, or all quotas spent | `dispatch opencode --model <local>/<model>` | runs on this machine, costs wall-clock only |

## Model tiers — senior / middle / junior

> **Superseded on this machine by the tier table in `~/.playmaker/policy.md` (owner order
> 2026-09-04): there is no middle tier there, and `agy` Claude models are junior. The rows below are
> the skill's generic defaults for a machine without a policy file.

**Judge a lane by the model actually serving it, never by the lane's name.** Model rosters move
faster than this file; re-derive the table whenever a lane surprises you, and correct it here.

| Tier | What is actually there | Give it |
|---|---|---|
| **Senior** | the coach's own session (Fable 5.1, falling back to Opus 5.5); **`codex`** — the real Codex CLI on the ChatGPT plan (`--model` omitted; verify with `codex --version`); **`opencode` / GLM** (`zai-coding-plan/glm-*`); **`kimi`** — Kimi K3 via `kimi -m kimi-code/k3-256k`; **`muse`** — Muse Spark via `muse` | architecture, spec interpretation, cross-module integration, adjudication, anything irreversible |
| **Near-senior** | **`agy gemini-3.1-pro-high`** — not quite the three above, but close enough to carry a review lens or a hard WP on its own | the always-on review seat, demanding implementation, deep recon |
| **Middle** | the rest of `agy` / Antigravity — its **Claude 4.6 / Sonnet 4.6** are a generation behind the frontier despite the name — plus GPT-OSS | well-specified implementation against a ready plan, refactors, CRUD by convention, a second opinion |
| **Reserve** | `dispatch claude --model sonnet` / `--model opus` — Sonnet 5.5 and Opus 5.5, senior capability on the coach's own subscription | nothing by default: tier is capability, allocation is the policy's call — a seat only on the owner's word |
| **Junior** | Gemini **Flash** tiers, Codex Spark | mass reading, codebase sweeps, mechanical diffs, tight fix→check loops |

Two traps this table exists to prevent — both have bitten in practice:

1. **A lane serving "Claude Opus" is not necessarily the current Opus.** Antigravity's Claude
   models trail the frontier by a full generation. Cheap, capable, worth using — but *middle*,
   not senior. Their verdicts are an input you weigh, not an authority you defer to.
   Conversely, do **not** file GLM as cheap because it is cheap: it is a senior lane.
2. **`codex` is the Codex CLI, not a Google model.** The lane name is not evidence of the engine.

**On irreversible work** — deletions, migrations, money paths, auth, public contracts — a middle
lane may never be the only senior-grade eye. Pair it with `codex`, or adjudicate that hunk yourself.

Match the WP to the tier, then reserve the most-depleted senior model for the lightest role —
usually the coach's own adjudication — and push volume down to junior lanes.

## Junior fan-out — where it is safe

Route down by default when **all** of these hold:

1. **Self-verifiable outcome** — a green command the worker runs itself. An objective gate replaces
   senior judgment.
2. **Tight file boundary plus a pattern to mirror** — CRUD by existing convention, a test scaffolded
   from a neighbouring spec, a story or doc from a template.
3. **Low blast radius** — one WP, dark or flagged code, no schema or contract change; a bad diff is
   cheap to throw away.
4. **Read-only by nature** — recon, sweeps, summarization, fact-check tables. Zero risk beyond
   wasted tokens.

## Where juniors are never safe

Regardless of how well specified — these stay with a senior lane or the coach:

- **Money paths**: billing, entitlement ledgers, IAP/Stripe webhooks, refunds, idempotency.
- **Schema migrations, backfills, destructive data operations.**
- **Auth, session, and security-sensitive code.**
- **Frozen public API contracts** and whatever the repo's policy marks human-review-required.
- **Anything without a one-sentence done-condition.** Juniors do not resolve spec ambiguity, they
  amplify it.

A junior diff that touches this list is a routing bug: pull the WP back, do not patch it in review.

## Escalation

A worker that fails its own gate **twice** escalates one tier — never a third attempt at the same
tier. Two failures at the top tier means the WP is wrong, not the worker: re-scope it.

## Two-stage senior review

Juniors produce the fact-check and consistency reports; seniors render verdicts *on top of those
reports* rather than re-reading the world. This is the cheapest way to buy senior judgment.
