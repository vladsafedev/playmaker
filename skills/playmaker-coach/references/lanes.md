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
   and `continue` independently. Same subscription; the lever is the **model bucket** — the mid tier
   is a separate weekly bucket from the top tier and usually idle, so default `--model sonnet`
   (`haiku` for trivial mechanical work). playmaker runs it in `acceptEdits`: it writes freely
   inside `--cwd` and is refused outside it, so keep every path in the prompt inside `--cwd`.

4. **External dispatch — `codex` / `agy` / `opencode` / `kimi`.** Each on its own subscription or
   plan — the home for write-heavy parallel implementation that can leave the Anthropic
   subscription.
   - **`agy` (Antigravity)** carries more than Google models: alongside Gemini Flash and Pro tiers it
     serves **Claude Sonnet/Opus (Thinking)** and a GPT-OSS tier. Its Claude runs on *Google's*
     pool and spends none of the Anthropic bucket — but that roster has trailed Anthropic's own
     releases by a generation (Claude 4.6 on agy while Claude 5 ships on Anthropic), so tier it by
     the version `agy models` shows, never by the name. Note the internal split: all Gemini models
     share one bucket, Claude and GPT-OSS share another.
   - **`opencode`** is the widest lane: one CLI over ~75 providers addressed as `provider/model` — a
     GLM coding plan, or a model running locally on this machine, which spends no subscription quota
     at all.
   - **`kimi`** runs the Kimi Code CLI on its own subscription: senior tier (K3), native login, no
     opencode.

**Never write an agy or opencode model name from memory** — run `agy models` / `opencode models` and
copy a line. Both rosters and their spelling move with releases, and playmaker validates `--model`
against the live roster, failing the dispatch on a stale name.

## Routing cheat-sheet

| The work… | Lane | Why |
|---|---|---|
| writes files, coach integrates the result directly | in-session sub-agent | write-capable, returns into context |
| is an independent stream to monitor separately | `dispatch claude --model sonnet` | tracked, detached, spares the top bucket |
| is heavy reasoning only the coach can do | coach | top tier, serial |
| is write-heavy and can leave Claude | codex / agy / opencode | their own quotas |
| needs a second strong reviewer without touching the Anthropic bucket | `dispatch agy --model <gemini-pro-high>` | near-senior judgment on an uncontended pool |
| is bulk work with every subscription low | `dispatch opencode --model <plan>/<model>` | a separate plan, untouched by the others |
| is mechanical and privacy-sensitive, or all quotas spent | `dispatch opencode --model <local>/<model>` | runs on this machine, costs wall-clock only |

## Tier-matching

- **Architectural / spec judgment / cross-module integration** → top tier (coach, top-tier Codex,
  Gemini-Pro-high on agy for review and advice rather than implementation).
- **Pattern-following implementation, scoped CRUD, mechanical refactor, test scaffolding, writing
  inside an existing convention** → mid tier (Claude Sonnet, Gemini-Pro-low, mid-tier Codex;
  agy's Claude models only when the roster shows a current version). Most delegated implementation lives here.
- **Recon, summarization, mechanical loops over many files, normalization** → cheap tier (Flash
  tiers, Haiku, fast Codex modes).

Reserve the most-depleted top-tier model for the lightest role — usually the coach's own
coordination. If the coach's own weekly is under ~50%, shrink its slice to design decisions and
final integration only.

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
