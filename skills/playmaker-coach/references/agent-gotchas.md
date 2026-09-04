# Per-agent traps

Read the entry for a lane before your first dispatch to it in a session, and whenever a dispatch
behaves strangely.

## Universal: proof on disk, not status

`playmaker` marks a session `done` when the process exits cleanly with text output. On builds with
the no-change check, a write task that touched nothing lands as **`no_changes`** — terminal, but not
success: it pings immediately even inside a batch and is excluded from the batch's completed count.
On older builds, check it yourself after every write task:

```bash
git -C "<cwd>" status --short      # empty tree on a write WP = NOT done
```

Batch summaries (`N/N done`) are transport truth, not result truth. Treat an empty tree exactly like
a failure: read `summary`, then decide `continue` versus a re-dispatch to another lane.

## Bad `--model` is the classic silent failure

playmaker catches both known shapes — a codex model/auth failure raises `codex turn failed: …`, and
an unknown agy model raises with the valid roster *before* dispatch. So a dispatch that comes back
failed with a model message means: fix the string, do not retry it. For agy and opencode, copy the
line from `agy models` / `opencode models` rather than typing it.

## claude (sibling)

- Runs with `--permission-mode acceptEdits`: it edits and runs commands freely **inside `--cwd`** and
  is refused outside it. A WP that legitimately needs a sibling repo or a dotfile in `$HOME` comes
  back refused — the fix is a different `--cwd`, not a re-prompt.
- Zero changes usually means one of: it tried to write outside `--cwd`, or the run needed a
  permission the configured mode does not grant (an "I need your permission" answer, not a crash).
- Default `--model sonnet`; omitting `--model` can put mid-tier work on the scarce top bucket.

## agy (Antigravity)

- The agent's shell lives in a **private scratch directory**, not the workspace. playmaker prepends a
  workspace preamble, but reinforce it: phrase file instructions as workspace-relative or absolute
  paths, **never "the current directory"**.
- A `done` with no file changes usually means the files landed in agy's scratch dir
  (`~/.gemini/antigravity-cli/scratch/`). Re-dispatch with explicit paths.
- Its own default model is top tier, so **always pass `--model`** on a dispatch meant to be cheap.
- Its **5-hour** windows are what a fan-out drains first; the Gemini family and the Claude/GPT family
  have separate ones.

## codex

- The model roster depends on the account plan, and an unavailable name fails the whole dispatch.
  **Omitting `--model` is the safe default here** — it uses whatever the account actually has.

## opencode

- Models are `provider/model` and the default is invisible: without `--model` it falls through to
  opencode's own last interactive pick, which is not written to its config file. Pin
  `[agents.opencode] model` in `~/.playmaker/config.toml`, or pass `--model` every time.
- **GLM drops the leading `/` of absolute paths.** `/tmp/x/hello.txt` becomes `tmp/x/hello.txt` and
  lands under `<cwd>/tmp/x/…`, while the agent reports "Wrote file successfully". So: never put an
  absolute path in an opencode prompt, open with *"Working directory is the repo root; use paths
  RELATIVE to it for every file operation, never absolute"*, still pass `--cwd`, and after `done`
  check for a directory named after the cwd's own path components (`<cwd>/Users/…`, `<cwd>/private/…`)
  — that is where the writes went.
- Second failure mode: on a large task it may produce ten minutes of good analysis and exit `done`
  with zero writes. Size opencode write tasks to roughly 100 lines of output or split them.
- Neither trap applies to **review** dispatches, which write nothing — which makes opencode a
  perfectly good reviewer even where it is a shaky implementer.

## kimi (Kimi Code CLI)

- There is **no read-only mode below the prompt**: `-p` refuses `--auto`, `--yolo` and `--plan`
  (exit 1) and already runs with auto-approval. Only dispatch work you would run unattended anyway.
- The session id arrives **only in the trailing `session.resume_hint` line**, so `playmaker list`
  shows the agent session late — do not conclude a dispatch failed just because the id has not
  appeared yet.
- Sessions are **per-cwd**: `kimi session list` from another directory shows nothing. Track the
  session through playmaker, not through the CLI.
- The stream carries **no token/cost fields** — there is nothing to budget against mid-run.
- Exit codes: **exit 1 is non-retryable** (auth, quota, unknown model — "is not configured in
  config.toml"); **exit 75 is retryable**. Fix the cause on 1, re-dispatch on 75.
- K3 is **slow on real tickets** — never `--sync` a big WP; dispatch detached and poll.
- Needs **Node ≥ 22.19** — hence the wrapper binary; point `[agents.kimi] binary` at it.
- **Always pass `-m kimi-code/k3-256k`** — the CLI default is the weaker K2.7 `kimi-for-coding`.

## Worktrees

Parallel WPs that touch the same files collide. Give each its own git worktree and dispatch with
`--cwd <worktree>`. In JS monorepos remember the worktree needs its `node_modules` (symlink the
store and repoint workspace packages) or the WP's gate silently cannot run — and a gate that cannot
run is a WP that was never verified.
