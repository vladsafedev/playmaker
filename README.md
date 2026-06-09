# playmaker

[![PyPI](https://img.shields.io/pypi/v/playmaker-cli.svg)](https://pypi.org/project/playmaker-cli/)
[![Python](https://img.shields.io/pypi/pyversions/playmaker-cli.svg)](https://pypi.org/project/playmaker-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A **playing-coach** CLI for orchestrating Claude Code, Codex, and Gemini sub-agents in parallel.

The coach (you, in your active Claude Code session) keeps doing your part of the work — and dispatches the rest as detached subprocesses to other AI CLIs. `playmaker` is the runner: it spawns them, tracks state, parses their session files, surfaces threads, and notifies on completion.

The intelligence — *when* to delegate, *which* agent gets *which* slice, how to review their output — lives in the `playmaker-coach` skill (Claude Code) that you install separately.

## Why

Claude Max + Codex + Gemini together = three independent quotas. A single Claude session that does everything serially burns weekly tokens. Decomposing into 3-5 parallel work-streams across providers is often 2-4× faster wall-clock and cheaper.

The catch: doing it manually (terminal tabs, jumping between tools, copy-pasting context) is friction. `playmaker` removes the friction; the skill provides the discipline.

## Install

### macOS (Homebrew)

```bash
brew tap shulyugin/playmaker
brew install playmaker
```

### Cross-platform (Python)

```bash
# uv (recommended)
uv tool install playmaker-cli

# or pipx
pipx install playmaker-cli
```

The `playmaker` binary lands in your PATH.

```bash
playmaker init
```

This creates `~/.playmaker/` with state.db, logs/, outputs/, agents/, and a default config.

## Prerequisites

`playmaker` orchestrates external CLIs — install whichever you have access to:

- **Claude Code** — `npm i -g @anthropic-ai/claude-code` (or download from claude.com/code)
- **Codex CLI** — `npm i -g @openai/codex`
- **Gemini CLI** — `npm i -g @google/gemini-cli`

`playmaker agents` will tell you which are reachable.

## Usage

```bash
playmaker agents                              # who's installed
playmaker quotas [--refresh]                  # capacity per provider

playmaker dispatch <agent> --prompt "..."
                  [--cwd <dir>]
                  [--files PATH...]
                  [--sync]                    # block until done; default is detached
                  [--parent <id>]
playmaker list [--status running|done|failed] [--agent NAME] [--json]
playmaker get <id> [--wait] [--json]
playmaker summary <id>                        # last 2 assistant messages
playmaker thread <id> [--last N] [--all] [--role assistant|user|tool]
                     [--include-tools] [--max-bytes N] [--json]
playmaker logs <id> [--follow]
playmaker kill <id>
playmaker watch                               # live TUI of sessions
```

## How it works

```
~/.playmaker/
├── state.db          SQLite — sessions, status, pids, output paths
├── config.toml
├── agents/           agent profile markdown (claude.md, codex.md, gemini.md)
├── outputs/          final assistant text per session
├── logs/             subprocess stdout for detached runs
└── quotas.json       latest capacity snapshot
```

Per-project profile overrides go in `./.playmaker/agents/<name>.md` next to your repo.

`dispatch` runs the agent's CLI non-interactively, parses its native JSON output, and locates the session file each tool writes locally. Empirically:

- Claude: `~/.claude/projects/<cwd-with-slashes-as-dashes>/<id>.jsonl`
- Codex: `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<thread_id>.jsonl`
- Gemini: `~/.gemini/tmp/<cwd-basename>/chats/session-<ts>-<short_id>.{json,jsonl}`

`thread`/`summary` parse those into a uniform turn list so you can read all three in the same shape.

## The coach skill

`playmaker` is a runner. The decision-making lives in [`playmaker-coach`](https://github.com/shulyugin/playmaker-coach) — a Claude Code skill that knows when delegation is worth the overhead, how to decompose tasks, and how to review sub-agent diffs.

Install:

```bash
# in your Claude Code skills directory
git clone https://github.com/shulyugin/playmaker-coach ~/.claude/skills/playmaker-coach
```

Then in any Claude Code session, give a multi-component task and the skill activates.

## Quotas

Token-based capacity probes. Status as of v0.2:

- **Claude** — full support via `claude /usage` (PTY parse).
- **Codex** — stub. Their quota lives behind an undocumented API; web-scraping fragile.
- **Gemini** — stub. Same issue.

Codex/Gemini availability is treated as "unknown" until you hit a rate-limit error. Contributions for proper probes welcome.

## Limitations

- macOS-only Claude-quota probe (Keychain/PTY parse of `claude /usage`). Everything else works on Linux.
- No remote agents. Everything runs locally on your machine.

## License

MIT. See [LICENSE](LICENSE).
