# Reading quotas

```bash
playmaker quotas              # re-probes itself once the snapshot passes [quotas] max_age (5m)
playmaker quotas --refresh    # probe now regardless
playmaker quotas --cached     # print the stored snapshot without probing
```

The header prints the snapshot's age. If it ever reads *stale*, the probes did not run — say so in
the plan instead of quoting the numbers.

**Read the table at model granularity, not provider granularity.** One provider commonly exposes
several tiers with independent buckets, and the whole point of pulling quotas is to push work
*away from* the depleted bucket and *toward* the fresh one.

- **Claude:** two weeklies — the Fable-scoped one (the coach's own session) and the all-models one
  (Opus, Sonnet, sub-agents: the coach's fallback). Sonnet has had no bucket of its own since
  September 2026, so `dispatch claude` spends the coordinator's reserve. The probe has answered
  `invalid_grant` since 2026-09-24 — read `/usage` until the login is renewed.
- **Antigravity (`agy`):** one Google pool split by family — `Gemini 5h` / `Gemini weekly` and
  `Claude/GPT 5h` / `Claude/GPT weekly`. All Gemini models share the first; Claude *and* GPT-OSS
  share the second. So one Gemini reviewer plus one agy-Claude reviewer costs one hit in each of two
  separate buckets — the cheapest way to buy two independent opinions. Requires agy's local daemon;
  if the table says "daemon offline" it fell back to a coarse Gemini-only view.
- **Codex:** top-tier versus lighter modes, where the account plan carries them.
- **opencode:** the quota belongs to the *plan behind the provider*, not to the CLI — it appears
  under that provider (e.g. a GLM coding plan's session and weekly credit windows) and reads
  unsupported without a credential. A dispatch pointed at a **local** model spends nothing and never
  appears in the table.
- **Muse Code:** two buckets — `Session` (the rolling 5-hour window) and `Weekly`, both
  percentage-based. Meta omits usage while the 5-hour window is idle: the block then shows the plan
  name with a note instead of bars (weekly stays unknown until the next prompt spends something),
  so no bars does not mean no quota. The credential is the CLI's own login, read-only — inline in
  `~/.config/muse/auth.json`, else the macOS Keychain item `ai.meta.dev.credentials` via
  `security`; the first Keychain read can raise a macOS "allow access" dialog, and "Always Allow"
  makes later probes silent.

## Rules

- Skip a *model* whose remaining capacity is under ~10%; reroute to another model on the same agent
  before switching agents.
- The **5-hour** windows are what bite during a fan-out — a burst drains them well before the
  weekly. If a 5h window is low, spread the burst or move slices to another provider.
- If a top-tier weekly is degrading toward a deadline, push everything possible to the mid and cheap
  tiers of the *same* provider, which are usually nowhere near depleted.
- State per-model capacity in the plan proposal, so the user can correct the routing.
- `playmaker quotas --refresh` right before a fan-out on shared pools — the 5-minute cache hides a
  window another session drained.

## Level-loading — the tie-breaker, not the objective

Capacity does not roll over. A pool that ends the week at 100% is capacity that was paid for and
never used, while the coach's own bucket did the work. The objective is accepted work per point of
scarce quota, with prepaid capacity spent before it resets. Level-loading — every pool drawn down
roughly evenly by week's end, except the one reserved for the coach — decides between lanes that fit
a WP equally (policy: fit before headroom). It never puts a WP in a queue behind a closed lane: a
closed or busy lane is replaced now, not waited for.

Practically, inside a fan-out:

- Among the lanes that fit the WP, sort by remaining headroom and deal work off the top, round-robin. Two WPs in a row should
  not go to the same pool while another sits full.
- Treat a pool that others also use as *shared*, not *forbidden*: give it work at its share of the
  load, and check the 5h window before a burst rather than avoiding it on principle.
- A pool nobody else touches is the first place to put bulk work, not the last.
- When one lane must be dropped mid-fan-out (a drained 5h window), name the substitute in the plan
  or the board rather than silently re-routing everything to one survivor.
