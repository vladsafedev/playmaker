# Prompt templates

Copy, fill the angle brackets, delete what does not apply. The shape matters more than the wording:
scope, gate, done-condition, context.

## Work package (implementation)

```
<one sentence: what to build and why it exists>

SCOPE — edit ONLY these files:
  <path/a.ts>
  <path/b.spec.ts>
Do not touch anything else. If the change appears to require another file, stop and say so in your
final answer instead of editing it.

CONTEXT you need (do not go looking for more):
  - Mirror the pattern in <path/neighbour.ts>.
  - Convention: <naming / error handling / logging rule>.
  - Spec: <the two or three sentences that actually constrain this>.

ACCEPTANCE — run this yourself and keep iterating until it exits 0:
  <cmd, e.g. npx tsc --noEmit -p <tsconfig> && npx eslint <paths>>
Paste the final output of that command into your answer.

DONE when: <one sentence the coach can confirm in seconds>.

Report at the end: files changed, the gate command's exit status, and anything you decided that the
prompt did not specify.
```

## Reviewer

```
You are reviewing one work package. You did NOT write it. Your job is to REFUTE it, not to
summarize it. Change nothing — this is a read-only review.

WHAT WAS ASKED:
<the WP's scope, acceptance criteria and done-condition, verbatim>

THE DIFF UNDER REVIEW: <.playmaker/reviews/<wp>/diff.patch>
(read it with `git apply --stat`/`cat`; the working tree at <cwd> holds the applied result)

YOUR LENS: <correctness & regression | contracts & integration | <risk> | conventions & tests>
Review through that lens first. Note anything outside it only if it is blocking.

Re-run the acceptance gate yourself: <cmd>. Report its real exit status — do not trust the
implementer's claim.

Rules:
  - Every finding needs file, line, and a concrete failure scenario (inputs or state -> wrong
    behaviour). No evidence, no finding.
  - "blocking" = the code is wrong: a bug, a broken contract, a security or data-integrity hole, or
    a missed acceptance criterion. Not "I would have written it differently".
  - Style preferences are "minor" and never block.
  - If you find nothing blocking, say so — a clean pass is a legitimate result. Do not invent
    findings to look thorough.

OUTPUT: exactly one JSON object, no prose before or after, matching this shape:
<paste the verdict contract from references/review-board.md>
```

## Fix list (follow-up to the implementer)

Send with `playmaker continue <impl-id>` — the worker still has its context, so do not re-paste the
WP or the file contents.

```
Review found <N> blocking issues. Fix exactly these, nothing else:

1. <file:line> — <what is wrong> -> <what it must do instead>
2. <file:line> — <...>

Do not refactor anything not listed. Re-run <gate cmd> until it exits 0 and paste the output.
Reply with a one-line note per item saying what you changed.
```

## Recon (read-only)

```
Recon only — change no files.

Find, in <dir>:
  (a) <thing>
  (b) <thing>
  (c) <thing>

Report under 200 words as a numbered list, each item with file path and line range. No code blocks,
no recommendations, no summary of the codebase at large.
```

## Arbiter (only when two reviewers disagree)

```
Two reviewers disagree about one hunk. Decide which is right.

THE CODE: <file:line-range>, in the diff at <path to patch>
REVIEWER A says: <claim + scenario>
REVIEWER B says: <claim or "no finding here">

Determine whether A's scenario actually occurs in this code. Trace the path. Change nothing.
Answer as JSON: {"winner": "A|B", "reason": "<two sentences>", "confidence": "high|medium|low"}.
```
