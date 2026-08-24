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

- **Claude:** the top-tier weekly and the mid-tier weekly are **independent**. The coach and
  in-session sub-agents spend the top one; the mid tier usually sits idle. Push mid-tier work there
  instead of burning the scarce bucket.
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

## Rules

- Skip a *model* whose remaining capacity is under ~10%; reroute to another model on the same agent
  before switching agents.
- The **5-hour** windows are what bite during a fan-out — a burst drains them well before the
  weekly. If a 5h window is low, spread the burst or move slices to another provider.
- If a top-tier weekly is degrading toward a deadline, push everything possible to the mid and cheap
  tiers of the *same* provider, which are usually nowhere near depleted.
- State per-model capacity in the plan proposal, so the user can correct the routing.

## Level-loading — the point of reading the table

Capacity does not roll over. A pool that ends the week at 100% is capacity that was paid for and
never used, while the coach's own bucket did the work. So the goal is not to *hoard* the pools that
someone else also draws on — it is to finish the week with every pool drawn down roughly evenly,
except the one reserved for the coach itself.

Practically, inside a fan-out:

- Sort lanes by remaining headroom and deal work off the top, round-robin. Two WPs in a row should
  not go to the same pool while another sits full.
- Treat a pool that others also use as *shared*, not *forbidden*: give it work at its share of the
  load, and check the 5h window before a burst rather than avoiding it on principle.
- A pool nobody else touches is the first place to put bulk work, not the last.
- When one lane must be dropped mid-fan-out (a drained 5h window), name the substitute in the plan
  or the board rather than silently re-routing everything to one survivor.
