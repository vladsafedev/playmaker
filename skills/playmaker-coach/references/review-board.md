# The review board

Every work package that changes code is reviewed by **agents**, in parallel, before it lands. The
coach reads verdicts, not diffs. This file is the whole protocol; `scripts/review-board.sh` is the
fan-out.

## Why agents and not the coach

Reading a diff carefully is the single most expensive thing the coach can do with its context, and
it is a task with an objective output — findings with evidence. That makes it delegable in exactly
the way implementation is. What is *not* delegable is arbitration: deciding which findings are real,
which are style, and what the worker must change. That stays with the coach, and it is cheap,
because it operates on structured verdicts instead of source.

## Composition

Pick per WP risk class. Repo policy (`./.playmaker/policy.md`) may override the lanes; it may never
lower the counts.

| Risk | What it covers | Reviewers |
|---|---|---|
| `routine` | stories, docs, tests, copy, styling, generated code | 1 reviewer + the WP's own gate |
| `normal` | ordinary feature and refactor work across a few files | 2 reviewers, different lanes |
| `high` | money paths, entitlement/billing, auth/session, schema migrations, public API contracts, anything the repo marks protected | 3 reviewers, different lanes, at least one top tier |
| `seams` | the joints between ≥2 WPs of one fan-out, after each passed its own board and the global gate: changed interfaces, shared types, migration order vs. consumers, event and config names, shared state | 1 fresh senior lane that implemented none or the least of the fan-out; the roster's second line serves when the first is the implementer |

Hard rules on composition:

- **The implementing session never reviews its own WP.** A *different session* of the same model is
  acceptable when lanes are scarce, but a different lane is always better.
- **Reviewers must not see each other's verdicts.** Independent dispatches, no shared thread. Two
  reviewers that read one another collapse into one opinion with extra steps.
- **Pick reviewers from different quota buckets** so a fan-out cannot drain one window. Two lanes on
  the same provider are fine when the provider splits its buckets by model family.
- **Reviewers run `--read-only`.** They produce findings, never fixes. A reviewer that edits code has
  destroyed the independence you paid for.

## Lenses

Give each reviewer a distinct lens in its prompt. Redundant reviewers find redundant bugs; diverse
lenses find different classes. Assign in this order as the count grows:

1. **Correctness & regression** — does it do what the WP says, and what did it break? Edge cases,
   error paths, null/empty/boundary, concurrency, idempotency.
2. **Contracts & integration** — types, API shapes, DB schema, migrations, event payloads, callers
   the WP forgot, backwards compatibility.
3. **Risk lens, chosen by WP** — security/authz for auth work, money-flow correctness for billing,
   data-loss for migrations, performance for hot paths.
4. **Conventions & tests** — does it match the repo's patterns, is the test coverage real or
   decorative, does it leave dead code or TODOs behind.
5. **Seams** (only on a `seams` board) — the joints listed in the spec, never the internals of a
   package that already passed its own board.

**Match the lens to what the lane can do, and make `risk` falsify.** A seat that cannot execute
binaries (the `claude` lane runs with all of them forbidden) cannot hold `correctness` — its
`gate_rerun` is always `NOT RUN`; give it `contracts` or `conventions`, and let a lane that can run
the gate hold correctness. On `high`, the `risk` seat is asked to falsify the acceptance criteria: a
command, input or repro per criterion it attacked, not only an argument — a `pass` from that seat
lists what it tried.

## The verdict contract

Every reviewer returns **exactly one JSON object and nothing else**. This is what makes adjudication
cheap and what lets the script collect verdicts mechanically.

```json
{
  "wp": "<wp-label>",
  "reviewer": "<lane/model>",
  "lens": "correctness|contracts|risk|conventions",
  "verdict": "pass|pass_with_nits|fail",
  "findings": [
    {
      "severity": "blocking|major|minor",
      "file": "path/relative/to/repo.ts",
      "line": 42,
      "claim": "one sentence: what is wrong",
      "scenario": "concrete inputs or state -> wrong output, crash, or broken invariant",
      "suggested_fix": "one sentence",
      "confidence": "high|medium|low"
    }
  ],
  "gate_rerun": "the acceptance command the reviewer ran and its exit status, or null",
  "unverifiable": ["things the reviewer could not check and why"]
}
```

Rules the prompt must state explicitly:

- **A finding without `file`, `line` and a concrete `scenario` is not a finding.** Vague unease
  ("this could be cleaner", "consider extracting") is `minor` at best and usually noise.
- **`blocking` means the WP is wrong**, not that the reviewer would have written it differently:
  a bug, a broken contract, a security or data-integrity hole, a missing acceptance criterion.
- **Style preferences are `minor` and never block.**
- The reviewer's job is to **refute** the implementation against the WP's acceptance criteria, not
  to summarize it. Told to summarize, a model finds nothing; told to break it, it finds real bugs.
- Reviewers may run read-only commands (tests, typecheck, `git log`) and should re-run the WP's gate
  themselves — a gate that only passed in the implementer's session is a common failure.

## Adjudication — the coach's part

1. Read the JSON verdicts only. Do not open the diff yet.
2. **Drop** findings without evidence, and findings that restate the WP's own accepted trade-offs.
3. **Deduplicate** across reviewers by `file:line` + claim; two independent reviewers on the same
   finding raises its priority.
4. **Resolve disagreement yourself.** One reviewer blocking and another passing on the same code is
   normal — go look at *that one hunk* (this is the only diff reading the coach does) and decide. If
   it is genuinely ambiguous, a third reviewer as arbiter beats a coin flip; do not simply forward
   both opinions to the implementer, that is your job, not theirs.
5. **Only `blocking` findings gate landing.** `major` goes to the fix list if cheap, otherwise to a
   follow-up task. `minor` is recorded in the board and dropped unless the user wants it.
6. Write the fix list as **numbered, imperative, file-anchored instructions** and send it with
   `playmaker continue <impl-id>` — the worker still has its context, so do not re-paste the WP.

## The cycle

```
gate green -> review round 1 -> adjudicate -> continue(impl) with fix list
           -> gate green -> review round 2 (delta only) -> adjudicate -> land
```

- **Round 2 reviews the delta** only if you give the script the round-1 state as `<base-ref>` — a
  wip commit in the worktree (squash before landing) or a `git commit-tree` snapshot of the tree after
  round 1. With the original base the patch is cumulative, reviewers re-open settled code, and a fix
  cycle is spent on opinions. Update `spec.md` first when a ruling changed the scope.
- **Two fix cycles maximum on the implementing lane.** Blocking findings still standing after the
  second fix go to the escalation seat (Codex Sol per policy) for the third cycle, with both verdicts
  and the fix list in a fresh prompt. Inform the user in the board summary; do not ask them. A
  dispatch that died of quota or auth is not a round.
- **Land at zero blocking.** Record `major`/`minor` leftovers in the board so they are not lost.

## Precheck

Before any reviewer is dispatched, the script runs one cheap **precheck**: does the diff actually
cover the spec's acceptance list? On 2026-10-18 half of the first review rounds failed on exactly
this — the worker delivered fewer tests than the acceptance list demanded — and each such round
cost two senior reviewers ~15 minutes and a full fix cycle. A cheap lane answers the same
question in half a minute, so the board never starts until that check passes.

The precheck reads the spec's `## ACCEPTANCE` section and `## DONE when` line plus the round's
`diff-rN.patch`, and returns one JSON object: `covered` (item + evidence in the diff), `missing`
(item + why), `verdict`. The diff is the only evidence — an acceptance item with no evidence in
the diff is missing; style is never a reason.

Roster: the `precheck` risk-class keyword in `reviewers.conf` — an ordered preference list of
cheap lanes maintained by the coach (the script never hard-codes models); the first lane to
answer wins:

    precheck opencode zai-coding-plan/glm-5.3-flash     precheck
    precheck agy      gemini-3.8-flash-low              precheck

Behaviour and exit codes of a dispatch run:

- **pass** — `precheck: pass (<lane>, <seconds>s)`, the board is dispatched as today. The answer
  lands in `precheck-rN.json`, the status in `board.env` (`precheck=pass`, `precheck_lane=…`).
- **fail** (missing items) — the script prints the numbered fix list (`N. <item> — <why>`),
  writes `precheck=fail`, and exits **2** without dispatching a single reviewer. Send the list
  to the worker with `playmaker continue <impl-id>` — this counts as a fix cycle exactly like a
  board verdict, not as a review round.
- **unavailable** (every lane failed, timed out after 120 s, or returned no parseable JSON) —
  the script warns `precheck: unavailable (<reasons>)` and CONTINUES to the board. The precheck
  must never block the pipeline by being down.

Flags: `--no-precheck` skips it; `--precheck-only` runs it alone and exits with its status
(0 on pass/unavailable, 2 on fail); `--dry-run` prints the precheck prompt too. A spec without
an `## ACCEPTANCE` section makes the precheck unavailable — the board runs as before.

## Automatic loop

Everything above that is mechanical for the common case — waiting for the worker and its gate,
the precheck, the wip snapshot, the fan-out, waiting for seats, collecting, filtering verdicts,
writing the fix list, sending it, running the next round on the delta — is one command:

```bash
pm-review --auto <wp-label> <base-ref> --impl-id <impl-session> --paths "<wp pathspecs>" \
    [--risk routine|normal|high|seams] [--gate "<cmd>"] [--impl-agent <lane>] \
    [--max-cycles N] [--seat-deadline 20m] [--dry-run]
```

`--paths` is **required**: the wip commits and the `git add -N` cover the WP's files only —
in a shared worktree an unpinned commit would sweep other packages' work into this WP's
history.

It runs in the **foreground** until the loop ends — background it (`nohup pm-review --auto … &`
or under Monitor) and read the final summary it prints; every step is appended with a timestamp
to `.playmaker/reviews/<wp>/auto.log`. The loop:

1. **Wait the implementer** (`playmaker get --wait`). `failed`/`killed`, or `no_changes` on a
   write task → **exit 4**, reroute. A `gate_status` of `fail`/`timeout` is handled whether or
   not `--gate` was passed — the gate command comes from `--gate`, else from the implementer
   session's own `gate_cmd`, else exit 4 «implementer gate failed and no gate command is
   known» — and the tail of the CLI's gate log goes back as a fix list
   (`continue --after --gate --expect-changes`); that consumes a cycle and the check repeats.
   A failed gate never reaches the board.
2. **Precheck** as above, per round — always against the **whole package** (the original
   base): acceptance items describe the WP, not the round's delta, so a delta-based
   precheck would mark everything delivered in round 1 as missing on every later round.
   The reviewers still get the delta only. A fail goes back the same way and also
   consumes a cycle. New files get `git add -N` right before the precheck's diff is
   built — only after the implementer is done and its gate passed, so a failed
   implementer never leaves the index dirty.
3. **Wip snapshot** after the precheck passes: `git commit -m "wip: <wp> rN (auto)"` of
   the WP paths only. Round N diffs the previous wip commit, so later rounds review the
   delta — squash the wip commits before landing.
4. **Dispatch the board** as today. When the implementer's row carries `gate_status=pass`, the
   reviewer prompt says the CLI already ran the gate (with the log path) and asks the reviewer
   NOT to re-run it; otherwise the re-run instruction stands.
5. **Wait every seat** in **one common `--seat-deadline` window counted from the board
   dispatch** — each seat, and a substitute, waits only the time remaining in that window,
   so a hung board cannot stretch to N × the configured deadline. A `failed`/`killed`/
   timeout seat is substituted **once** from the roster's first not-yet-used eligible line
   (same lens, `--read-only`), else recorded as a missing seat. A `[limits]` refusal
   (`max_parallel=`/`min_free_ram_gb=`) on any dispatch — a seat, the precheck or the fix
   follow-up — is temporary: the loop logs «waiting for a lane slot» and retries every 30 s
   up to the seat deadline; only then is the seat dead (and substituted — a seat the board
   could not dispatch at all is substituted the same way).
6. **Mechanical adjudication**: `blocking` findings only; one without `file`+`line`+`scenario`
   is dropped; one on a file outside the round's diff is dropped as environmental; dedupe by
   `file:line` keeping the longer scenario. Zero blocking **and** a full board →
   `auto=green cycles=<n>` in `board.env`, summary, **exit 0**.
7. Otherwise the fix list (numbered `file:line — claim -> suggested_fix` + the standard tail)
   goes back via `continue --after`; the follow-up's gate result loops step 1, and round N+1
   reviews the delta.

Exit codes: **0** green · **3** stopped — the cycle budget is exhausted with blocking findings
left, the board could not fill its seats, or a fix round changed nothing (escalate per policy:
Codex Sol) · **4** the implementer failed, or a hard dispatch failure (unknown lane, auth,
binary) with the reason — reroute or fix the roster line. `--max-cycles` (default 2) and
`--seat-deadline` (default `20m`) come from `[board]` in `~/.playmaker/config.toml` or the
repo's `.playmaker/config.toml` (repo wins); flags override. `--dry-run` prints the plan —
steps, seats and reserves, deadlines, the fix-list format — and dispatches nothing. A hidden
`pm-review --adjudicate <wp>` runs step 6's mechanical pass alone over the verdicts and the
newest `diff-r*.patch` and prints the fix list — handy for demos and spot checks.

What stays with the coach: arbitration of reviewer disagreements, spec rulings, the escalation
seat, and any OWNER decision — the loop stops (exit 3/4) rather than deciding those.

## Running it

Write the WP spec to `.playmaker/reviews/<wp-label>/spec.md` first — scope, acceptance criteria and
the done-condition. The script refuses to run without it, because a reviewer cannot refute what was
never specified.

A WP's spec.md starts with a three-line header the ledger hook parses mechanically:

```
class: mechanical|feature|terminal-heavy|repo-recon|architecture|high-risk
impl: <lane> <model>
risk: routine|normal|high|seams
```

`gate: <cmd>` is optional and may precede them. The header feeds the ledger row the PostToolUse hook
writes at `git commit` (see `references/ledger.md`); a spec without `class:` makes the script warn
and the ledger record `class=-`.

```bash
pm-review <wp-label> <base-ref> [--risk routine|normal|high|seams] [--gate "<cmd>"] \
    [--impl-agent <lane>] [--paths "<glob> <glob>"] [--cwd <dir>] [--round N] [--dry-run]
```

`pm-review` is `scripts/review-board.sh` from this skill, symlinked onto `PATH`; call the script by
its full path if the symlink is missing.

New files first: `git diff` does not see untracked files — `git add -N <file>` for every file the WP
created, and the script warns about the ones it finds. Copy the WP's files aside before the board and
compare `git diff --stat -- <paths>` with `diff-rN.patch` after `--collect`, before the commit: a
"read-only" reviewer has reverted a tree mid-review before (21.09).

The script writes `.playmaker/reviews/<wp-label>/` with the diff under review, one prompt and one
verdict file per reviewer, `board.env` (risk, round, required count), and dispatches them as one
`--batch review-<wp-label>-rN`. Roster lines are an ordered preference list: after skipping the
implementer it seats the first 1 / 2 / 3 / 1 for routine / normal / high / seams and holds the rest in
reserve; fewer than required stops the script. On a new dispatch it archives the previous round's
verdicts to `archive-<timestamp>/`. When the batch
drains, collect them:

```bash
pm-review --collect <wp-label>     # writes verdict-*.json and prints every blocking finding
```

`--collect` marks a `failed` or `killed` session DEAD instead of waiting for it, prints
`verdicts: k/N`, and says `BOARD INCOMPLETE` while k < N — a board is not green on a partial count
unless board.md records why. If a reviewer returned prose instead of JSON, that reviewer failed — `playmaker continue` it once
with "return only the JSON object per the contract", and if it fails again, drop it and note the
reduced count in the board rather than pretending it passed.
