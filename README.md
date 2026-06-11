# playmaker

[![PyPI](https://img.shields.io/pypi/v/playmaker-cli.svg)](https://pypi.org/project/playmaker-cli/)
[![Python](https://img.shields.io/pypi/pyversions/playmaker-cli.svg)](https://pypi.org/project/playmaker-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A **playing-coach** CLI for orchestrating Claude Code, Codex, and Gemini sub-agents in parallel.

The coach (you, in your active Claude Code session) keeps doing your part of the work — and dispatches the rest as detached subprocesses to other AI CLIs. `playmaker` is the runner: it spawns them, tracks state, parses their session files, surfaces threads, and notifies on completion.

The intelligence — *when* to delegate, *which* agent gets *which* slice, how to review their output — lives in the `playmaker-coach` skill (Claude Code) that you install separately.

## Why

Three reasons to fan work out across agent CLIs instead of grinding through it in one serial session:

1. **Wall-clock speed.** A task that decomposes into 3–5 independent work-streams (schema, backend, frontend, tests, docs) finishes 2–4× faster when each stream runs as its own parallel agent.
2. **Provider arbitrage.** Codex and Gemini quotas are entirely separate pools from your Anthropic plan. Every slice you hand them is capacity your main session never spends.
3. **Otherwise-idle credit.** Headless `claude -p` — what playmaker dispatches — doesn't draw from your interactive Claude subscription. It bills the **Claude Agent SDK credit** included with paid plans (Pro $20 / Max 5x $100 / Max 20x $200 per month, metered at API rates). If you're not running headless agents, that credit sits unused; playmaker puts it to work without touching your session limits.

The catch: doing it manually (terminal tabs, jumping between tools, copy-pasting context) is friction. `playmaker` removes the friction; the skill provides the discipline.

## ⚠️ Sub-agents skip permission prompts by default

By default, playmaker launches Claude sub-agents with `--dangerously-skip-permissions` (and Gemini with `--yolo`). This is deliberate: a headless agent has no human at the keyboard, so without these flags a detached run stalls at the first tool-approval prompt and finishes having written nothing.

It also means a dispatched agent can run commands and edit files **without asking**. Only dispatch prompts you'd be comfortable running unattended, in working directories you trust.

To opt out for Claude, set this in `~/.playmaker/config.toml`:

```toml
[agents.claude]
skip_permissions = false
```

— with the caveat above: detached runs will then stall on the first permission prompt, so this only really makes sense alongside `--sync` workflows or allowlist rules you've configured in Claude Code itself.

## Install

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
                  [--batch <label>]           # group a fan-out; one summary ping
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
├── outputs/          final output per session — .md, or .json when the agent returned JSON
├── logs/             subprocess stdout for detached runs
└── quotas.json       latest capacity snapshot
```

Per-project profile overrides go in `./.playmaker/agents/<name>.md` next to your repo.

`dispatch` runs the agent's CLI non-interactively, parses its native JSON output, and locates the session file each tool writes locally. Empirically:

- Claude: `~/.claude/projects/<cwd-with-slashes-as-dashes>/<id>.jsonl`
- Codex: `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<thread_id>.jsonl`
- Gemini: `~/.gemini/tmp/<cwd-basename>/chats/session-<ts>-<short_id>.{json,jsonl}`

`thread`/`summary` parse those into a uniform turn list so you can read all three in the same shape.

## Notifications

Every detached dispatch pings when it finishes. With [`terminal-notifier`](https://github.com/julienXX/terminal-notifier) installed (`brew install terminal-notifier`), notifications are **clickable** — clicking opens the agent's output file in your editor. The editor is the `OPEN_WITH_APP` constant in `notify.py` (currently `"Zed"`). Without terminal-notifier, playmaker falls back to plain `osascript` banners, which can't be clicked.

**Batches.** Pass the same `--batch <label>` to every dispatch in a fan-out and per-dispatch success pings are suppressed — one "N/N done" summary fires when the whole batch drains. Failures are the actionable event, so they still ping immediately, with a distinct sound (Basso vs. the usual Blow).

Each session's final output lands in `~/.playmaker/outputs/<id>.md` (or `.json` when the agent returned genuine JSON), so the notification click — and `playmaker get` — always have a stable file to open.

## The coach skill

`playmaker` is a runner. The decision-making lives in [`playmaker-coach`](https://github.com/vladsafedev/playmaker-coach) — a Claude Code skill that knows when delegation is worth the overhead, how to decompose tasks, and how to review sub-agent diffs.

Install:

```bash
# in your Claude Code skills directory
git clone https://github.com/vladsafedev/playmaker-coach ~/.claude/skills/playmaker-coach
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
