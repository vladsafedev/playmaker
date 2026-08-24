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

- **Round 2 reviews the delta**, not the whole WP: pass the diff between the round-1 and round-2
  states. Re-reviewing untouched code wastes a full round and invites new opinions on settled code.
- **Two rounds maximum.** Blocking findings still standing after round 2 → stop, and hand the user
  the WP, both verdicts, and your recommendation. Do not open a third round and do not quietly fix it
  yourself: two failed rounds means the WP was wrong, not the worker.
- **Land at zero blocking.** Record `major`/`minor` leftovers in the board so they are not lost.

## Running it

Write the WP spec to `.playmaker/reviews/<wp-label>/spec.md` first — scope, acceptance criteria and
the done-condition. The script refuses to run without it, because a reviewer cannot refute what was
never specified.

```bash
pm-review <wp-label> <base-ref> [--risk routine|normal|high] [--gate "<cmd>"] \
    [--impl-agent <lane>] [--paths "<glob> <glob>"] [--cwd <dir>] [--round N] [--dry-run]
```

`pm-review` is `scripts/review-board.sh` from this skill, symlinked onto `PATH`; call the script by
its full path if the symlink is missing.

The script writes `.playmaker/reviews/<wp-label>/` with the diff under review, one prompt and one
verdict file per reviewer, and dispatches them as one `--batch review-<wp-label>`. When the batch
drains, collect them:

```bash
pm-review --collect <wp-label>     # writes verdict-*.json and prints every blocking finding
```

If a reviewer returned prose instead of JSON, that reviewer failed — `playmaker continue` it once
with "return only the JSON object per the contract", and if it fails again, drop it and note the
reduced count in the board rather than pretending it passed.
