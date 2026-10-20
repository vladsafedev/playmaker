# Decision review — three senior answers to «what breaks»

`pm-review-decision` is a short, read-only review of one concrete artefact: a WP spec, a disputed
diff hunk, numbers behind a choice, or an owner-facing product decision. It is not a review board
and is never a gate: **it never stops the cores of a wave.** The coach may keep dispatching and
integrating independent cores while this advisory review runs.

## When the coach convenes it

Use it when one of these is true:

1. Before dispatch, a WP spec is class `architecture` or risk `high`.
2. Two reviewers disagree about one hunk.
3. The coach marks a decision `спорно`.
4. The owner asks for one.

Without the owner's word, use at most two decision reviews per day. The script reads
`.playmaker/decision-reviews/log.md` and warns on the third; it deliberately does not refuse a
useful review.

## Run

Write a compact Markdown question containing the artefact, explicitly labelled options, and one
question. Then fan out the senior seats:

```bash
pm-review-decision <slug> --question <question.md> [--lanes codex,opencode,muse] [--deadline 10m] [--opus]
```

`pm-review-decision` is `scripts/review-decision.sh`. It takes the first three `decision` rows in
the roster, or the requested comma-list; every row carries a lens. `--opus` additionally seats the
first `decision claude ...` row and prints the reminder that it draws the coordinator's bucket.
Fewer than two seats is an error: one answer is an opinion, not a decision review. `--dry-run`
prints the selected seats and their wrapped prompts without dispatching.

The default is ten minutes; `[decision_review] deadline` in `.playmaker/config.toml` overrides the
personal config, and `--deadline` overrides both. Seats are dispatched in parallel, read-only, in
batch `decision-<slug>`. A failed or late seat is simply `no answer`, never substituted.

## Contract and result

Each seat returns exactly one JSON object:

```json
{"recommendation":"<option or free text, one sentence>","confidence":"high|medium|low","strongest_counter":"<one sentence>","breaks":[{"what":"…","where":"<artefact ref>","severity":"blocking|major|minor"}],"would_change_my_mind":"<one sentence>"}
```

The collector uses the last parseable, contract-valid object in a response. It writes raw output
and `answer-<lane>.json` under `.playmaker/decision-reviews/<slug>/`, then prints and saves
`summary.md`. Agreement means every valid recommendation starts with the same explicitly listed
option label, case-insensitively; free text is a split. Breaks are unioned by `where`, retaining
the highest severity. Exit `0` is agreement, `2` is split, and `1` means fewer than two valid
answers.

When the result is split, the coach decides; if owner input is truly needed, bring the owner the
split as **one** bounded question, with the strongest counter and the relevant breaks—not three
unfiltered model transcripts.
