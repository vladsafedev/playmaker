# The ledger and its hook

`~/.playmaker/ledger.jsonl` holds one JSON object per work package — the cross-repo evidence the
routing step reads. `scripts/ledger.py` maintains it (`add` / `fix` / `escape` / `stats` / `tail`);
it honours `PM_LEDGER` for tests. Rows whose note starts with "backfill" are historical — never
rewrite them.

## Fields

`date`, `repo`, `wp`, `class`, `risk`, `impl_lane`, `impl_model`, `gate_first_pass`, `reviewers`
(list), `blocking_r1`, `blocking_accepted`, `blocking_rejected`, `cycles`, `rounds`, `wall_min`,
`quota_note`, `outcome`, `commit`, `note`, `escaped_from` (list). Unknown fields are `-`; ints are
ints or `-`.

## How the hook maps a review dir to a row

On every `git commit` the PostToolUse hook (`scripts/ledger-hook.py`) matches the commit to the WP
under `.playmaker/reviews/` whose `diff-r*.patch` file list overlaps the commit's files the most
(ties → newest patch; dirs untouched for 14+ days are ignored), then fills:

| Field | Source |
|---|---|
| `date`, `repo`, `wp`, `commit`, `outcome` | today, toplevel basename, review-dir name, short sha, `landed` |
| `class`, `impl_lane`, `impl_model`, `risk` | spec.md header `class:` / `impl: <lane> <model>` / `risk:` (`routine|normal|high|seams`; see review-board.md); risk may also come from a pre-header spec title ("… risk high") or `board.env`; else `-` |
| `reviewers` | union of lanes over `sessions.txt` and every archived `sessions.txt`, short form (`kimi-k3`, `glm-5.3`, `agy-gemini-pro`, `codex`, `muse`, `claude-opus`) |
| `rounds` | number of `diff-r*.patch` |
| `cycles` | number of `fix-r<N>.md` with N ≥ 1 |
| `gate_first_pass` | `n` if `fix-r0.md` exists (the first gate did not pass), else `-` |
| `blocking_r1` | blocking findings across round-1 verdicts (oldest archive when rounds > 1), deduped by `file:line` |
| `blocking_accepted`, `blocking_rejected` | `-` (soft — the junior or the coach fills them) |
| `wall_min` | minutes from spec.md mtime to the commit |
| `note` | `auto-hook` plus the junior outcome |

A commit that matches nothing gets a one-line hint (`ledger.py add wp=… class=… impl_lane=…`); a
commit whose `(wp, commit)` row already exists is skipped. Every run is logged to
`~/.playmaker/logs/ledger-hook.log`. The hook never blocks or fails the commit.

## The junior

A junior model fills the fields the mechanics left as `-`; it never blanks or overrides a set value.
It runs as `codex exec -m gpt-5.3-codex-spark -s read-only -C <review dir> --skip-git-repo-check
--output-last-message <tmp>` with a 60 s timeout, reading spec.md, all verdicts, the fix lists,
board.env and the board.md line, and answering one JSON object. Configure it in
`~/.playmaker/config.toml` (or turn it off with `"none"`):

```toml
[ledger]
junior = "codex:gpt-5.3-codex-spark"
```

Any failure, timeout or non-JSON keeps the mechanical values; the reason lands in `note`.

## Fixing a row

```bash
python3 ~/.playmaker/scripts/ledger.py fix wp=<wp> [repo=<r>] [commit=<sha>] k=v …
```

updates the LAST matching row in place, validated like `add`. When the hook matched nothing, append
the row by hand with `ledger.py add`.

## Registering the hook (the coach edits ~/.claude/settings.json)

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "python3 <installed skill dir>/scripts/ledger-hook.py", "timeout": 120}
        ]
      }
    ]
  }
}
```
