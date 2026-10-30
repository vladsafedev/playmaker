# pm — playmaker beside the chat

A Claude Code mod for people who run [playmaker](https://github.com/vladsafedev/playmaker)
workers from a Claude Code session. It shows, carries and wakes; it decides nothing.

- **One pane.** At the top, every quota window of every lane as CodexBar shows it,
  in CodexBar's colors and order, in two columns. Below, every recent worker grouped
  by batch: a state glyph, the lane and model, elapsed time, the work it was given and,
  in small type, what is going on in its session (its last assistant message). Read
  every 3 seconds from `playmaker list --json`, every 5 minutes from
  `playmaker quotas --json`, session text every 30 seconds from `playmaker summary`.
- **Status line.** `pm · claude 52·Fable · codex 96 · kimi 85·wk …` under the prompt,
  and the same line as hidden context with every prompt you type.
- **Wake-up.** When playmaker records a finished batch that this session started — a
  `playmaker dispatch --batch …`, `review-board.sh` or `review-decision.sh` it ran
  itself, or a worker id its own call printed — the mod submits a brief to the
  chat — facts only: counts, workers, durations, directories, quotas — so the coach
  continues without anyone typing «check». Every other batch wakes nobody; a directory shared
  with other sessions is never enough. In-app toasts about workers and batches are off by
  default (`toasts`), since playmaker's own macOS notification already says the same.
- **Policy in the prompt.** The tiers section of `~/.playmaker/policy.md` and of
  `./.playmaker/policy.md` goes into the system prompt as written, once per session.
- **Quotas beside a dispatch.** After `playmaker dispatch …` runs, the model reads the
  current quota line next to the result. The dispatch itself is never refused.
- **`/pm` commands.** `board` (open or hide the pane), `quotas` (probe now),
  `review <wp> <base> …` (runs the coach skill's `review-board.sh --auto` detached and
  reports when it ends), `wp <id>` (the worker's summary into the prompt box), `policy`
  (re-read policy.md).

## Install

From GitHub, at the prompt of a Claude Code session:

```
/plugin install pm --marketplace vladsafedev/playmaker
```

From a pip/uv install of playmaker, a copy that matches the CLI's version:

```bash
playmaker mod install
```

From a checkout, read straight from the folder (edits apply on `/reload-plugins`):

```bash
claude plugin marketplace add /path/to/playmaker
claude plugin install pm@playmaker
```

Needs `playmaker` on PATH (or in `~/.local/bin`); `/pm review` needs the
`playmaker-coach` skill (`playmaker skill install`). Options (`/config`): wake on
batch, wake on a lone worker, quota refresh interval, policy in prompt, quotas
beside dispatch.

## Developing

```bash
claude plugin validate mods/pm
claude plugin test mods/pm
```

The hooks API is early access and may change between Claude Code releases; the
declarations the engine writes beside a loaded mod are the authority.
