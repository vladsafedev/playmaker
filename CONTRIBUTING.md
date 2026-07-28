# Contributing to playmaker

Thanks for your interest in improving `playmaker` — the playing-coach CLI that
dispatches sub-tasks to Claude Code, Codex, Antigravity, and opencode.

## Project layout

The package lives under `src/` (src-layout, `playmaker` package):

```
src/playmaker/
├── cli.py        Typer app — every `playmaker <command>` lives here
├── agents/       per-agent handlers (claude, codex, agy, gemini, opencode) + base Protocol
├── registry.py   name → handler lookup, profile discovery
├── state.py      SQLite-backed session store (~/.playmaker/state.db)
├── quotas.py     per-provider capacity probes
└── notify.py     macOS notifications for detached runs
```

## Local development

Requires **Python 3.11+**. We use [`uv`](https://docs.astral.sh/uv/), but `pipx`
works too.

```bash
git clone https://github.com/vladsafedev/playmaker
cd playmaker

# editable install (uv)
uv tool install --editable .

# or pipx
pipx install --editable .
```

Then initialize the local data dir (`~/.playmaker/` with state.db, logs/,
outputs/, agents/, config):

```bash
playmaker init
```

The `playmaker` entry point is defined in `pyproject.toml` as
`playmaker.cli:app`.

## Adding a new agent handler

Each agent is a handler that satisfies the `AgentHandler` Protocol in
`src/playmaker/agents/base.py`. Before writing code, work out three things
about the target CLI — they are what every handler is built around:

- how it runs **headlessly** (one shot, no TTY, no permission prompt);
- how it reports the **final assistant message**;
- where it writes its **session transcript**, and how the session id is
  recovered (some print it, `agy` only writes it to a debug log).

Then:

1. Create `src/playmaker/agents/<name>.py`.
2. Implement the `AgentHandler` Protocol — dispatch, resume, session-file
   discovery, and native-output parsing, as defined in `base.py`.
3. Register it in `src/playmaker/registry.py` so the CLI resolves it by name.
4. Add tests under `tests/` — parse a captured transcript fixture rather than
   invoking the real CLI, the way `tests/test_agy.py` does.
5. Add an entry to the config template in `cli.py` (`_DEFAULT_CONFIG`) if the
   agent needs settings, and document it in the README.

Match the existing handlers for the shape of session parsing — `thread` and
`summary` rely on every handler producing the same uniform turn list.

Optional per-agent profile markdown is discovered, not shipped: a user drops
`~/.playmaker/agents/<name>.md`, or `./.playmaker/agents/<name>.md` next to a
repo, and it is prepended to every dispatch for that agent.

## Coding conventions

- Target **Python 3.11+**; type-hint public functions.
- Lint and format with [`ruff`](https://docs.astral.sh/ruff/)
  (`line-length = 100`, `target-version = py311`, configured in
  `pyproject.toml`):

  ```bash
  ruff check .
  ruff format .
  ```

- Keep CLI output going through `rich`; keep provider-specific logic inside the
  relevant `agents/<name>.py` handler, not in `cli.py`.

## Running and testing

```bash
uv run pytest         # unit tests
uv run ruff check .   # lint

playmaker agents      # which agent CLIs are reachable
playmaker --help      # full command list
```

The test suite never shells out to a real agent CLI: handlers are exercised
against captured transcript fixtures and a faked `subprocess.Popen`. Keep it
that way so CI stays hermetic and free.

Before opening a PR, make sure `ruff check .` and `pytest` are clean, and that
`playmaker agents` plus a sample `playmaker dispatch` still work end to end
against whichever CLI you touched.

## Pull requests

Keep PRs focused and describe what you changed and how you verified it —
including which agent CLI and version you tested against, since almost
everything here is empirical about a third-party tool's behaviour.

Handlers for additional agent CLIs are especially welcome.
