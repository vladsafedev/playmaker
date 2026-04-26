# team

Multi-agent orchestration CLI — a thin facade for a Claude Code "playing coach" that dispatches sub-tasks to Codex and Gemini in parallel, monitors them, reads their threads, and reviews their diffs.

The CLI itself is a runner. The intelligence lives in the `team-coach` skill at `~/.claude/skills/team-coach/SKILL.md`.

## Install

```bash
cd ~/Sites/team
uv venv --python 3.13
uv pip install -e .
```

The `team` binary lands in `.venv/bin/team`.

```bash
team init
```

This creates `~/.team/` with state.db, logs/, outputs/, and a default config.

## Commands

```
team agents                                # who's installed
team quotas [--refresh]                    # capacity per provider

team dispatch <agent> --prompt "..."
                  [--cwd <dir>]
                  [--files PATH...]
                  [--detach]
                  [--parent <id>]
team list [--status running|done|failed] [--agent NAME] [--json]
team get <id> [--wait] [--json]
team summary <id>                          # last 2 assistant messages
team thread <id> [--last N] [--all] [--role assistant|user|tool]
                 [--include-tools] [--max-bytes N] [--json]
team logs <id> [--follow]
team kill <id>
team watch                                 # Rich live TUI
```

## Layout

```
~/.team/
├── state.db          SQLite — sessions, status, pids, paths
├── config.toml
├── agents/           agent profile markdown (claude.md, codex.md, gemini.md)
├── outputs/          final assistant text per session
├── logs/             subprocess stdout for detached runs
└── quotas.json       latest capacity snapshot
```

Per-project profile overrides live in `./.team/agents/<name>.md` next to your repo.

## Empirical notes

- Claude session files: `~/.claude/projects/<cwd-with-slashes-as-dashes>/<id>.jsonl`
- Codex session files: `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<thread_id>.jsonl`
- Gemini session files: `~/.gemini/tmp/<cwd-basename>/chats/session-<ts>-<short_id>.{json,jsonl}`
  (`.json` for non-interactive, `.jsonl` for interactive)

ClaudeProbe parses `claude /usage` output via PTY. Gemini and Codex quota probes are stubbed in v1 — their data lives behind undocumented APIs (Gemini) or web-scraping (Codex).

We rely on Zed's native "Import External Agent Threads" — `team` does not insert anything into Zed's database.
