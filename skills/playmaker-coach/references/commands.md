# Command surface

```
playmaker agents                                # who is installed and reachable
playmaker quotas [--refresh]                    # capacity, per provider and per model
playmaker dispatch <agent> --prompt "..."       # detached by default
                  [--model NAME] [--cwd DIR] [--files PATH...]
                  [--batch LABEL]               # group a fan-out into one summary ping
                  [--parent ID]                 # link lineage to an earlier session
                  [--read-only|--expect-changes]
                  [--sync] [--worktree NAME] [--worktree-base REF] [--gate "CMD"]
                  [--fallback LANE[,LANE]] [--prompt-file PATH]
playmaker continue <id> --prompt "..."          # follow-up inside the live session
                  [--model NAME] [--files ...] [--read-only|--expect-changes] [--sync]
                  [--after [ID]] [--gate "CMD"] [--prompt-file PATH]
playmaker events [--follow] [--batch LABEL] [--session ID] [--kind K] [--last N]
                 [--since ISO] [--interval S]   # JSON lines only
playmaker worktree ls [--repo DIR] [--size] | rm <NAME|PATH> [--force] |
                 prune-done [--into REF] [--dry-run]
playmaker list [--status running|done|failed|no_changes] [--agent NAME] [--limit N]
playmaker get <id> [--wait] [--poll SECONDS]
playmaker summary <id>                          # last 2 assistant messages
playmaker thread <id> [--last N] [--all] [--role assistant|user|tool]
                     [--include-tools] [--max-bytes N] [--follow]
playmaker logs <id> [--follow]                  # subprocess stdout for detached runs
playmaker kill <id>
playmaker watch                                 # live TUI
playmaker skill install [--dir PATH] [--force]
```

Every command takes `--json`.

- `--model` is forwarded verbatim to the agent's own CLI and stored on the session row, so
  `continue` and detached re-runs inherit it; `continue --model X` overrides one turn.
- `--batch LABEL` on every dispatch of one fan-out suppresses per-agent success pings and fires a
  single "N/N done" summary when the batch drains. Failures still ping immediately.
- `--read-only` marks recon and review dispatches so the zero-change check does not flag them;
  `--expect-changes` forces the check on an ambiguous write prompt.
- `continue` beats a fresh `dispatch` for "almost right, fix Y" — the session still holds the
  worker's reasoning, tool history and file context. Start fresh with `--parent <id>` only when that
  context has become a liability (requirements moved, the worker is looping).
- `--worktree NAME` creates an isolated `wp/NAME` worktree; `--worktree-base REF` selects its base.
  `--gate` runs after the agent exits and records `gate_status`; `--prompt-file` reads the prompt from disk.
- `--fallback LANE[,LANE]` reroutes quota, auth, or binary refusals within the fallback window.
  `continue --after [ID]` queues until ID is done; omitted ID means the session itself.
- `events --follow` is the Monitor wake stream; filter it by batch, session, kind, time, or interval.
  It emits JSON lines only.
- `worktree ls` inspects isolated trees, `rm` removes one, and `prune-done` removes completed trees.
  Use `--dry-run` before a broad cleanup and `--into REF` to choose its landing ref.
- `config.toml` supports `[limits] max_parallel` and `min_free_ram_gb`, `[gate] timeout`, `[worktree]
  setup_timeout`, `[events] interval`, and `[fallback] window`.
- `ledger.py add|fix --from-session <id>` fills `impl_lane`, `impl_model`, `wall_min`, and
  `gate_first_pass` from the session.

## Reading discipline

`summary` first — it usually answers "is it done and what does it claim". Escalate to
`thread <id> --last N` only when summary is insufficient, and to `--all --include-tools` only when
actively debugging why a worker went sideways. A long thread is tens of thousands of tokens;
`--max-bytes` is a safety cap, not a substitute for deciding what you need first.
