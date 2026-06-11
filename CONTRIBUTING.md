# Contributing to playmaker

Thanks for your interest in improving `playmaker` — the playing-coach CLI that
dispatches sub-tasks to Claude, Codex, and Gemini.

## Project layout

The package lives under `src/` (src-layout, `playmaker` package):

```
src/playmaker/
├── cli.py        Typer app — every `playmaker <command>` lives here
├── agents/       per-provider handlers (claude, codex, gemini) + base Protocol
├── state.py      SQLite-backed session store (~/.playmaker/state.db)
└── quotas.py     per-provider capacity probes
```

## Local development

Requires **Python 3.11+**. We use [`uv`](https://docs.astral.sh/uv/), but `pipx`
works too.

```bash
git clone https://github.com/shulyugin/playmaker
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

Each provider is a handler that satisfies the `AgentHandler` Protocol in
`src/playmaker/agents/base.py`. To add one:

1. Create `src/playmaker/agents/<name>.py`.
2. Implement the `AgentHandler` Protocol — the dispatch, session-file
   discovery, and native-output parsing methods defined in `base.py`.
3. Register the handler so the CLI can resolve it by name (in the agent
   registry alongside the existing claude/codex/gemini handlers).
4. Add the agent's default profile markdown under `agents/`.

Match the existing handlers for the shape of session parsing — `thread` and
`summary` rely on every handler producing the same uniform turn list.

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

Run the CLI straight from your editable install:

```bash
playmaker agents      # which providers are reachable
playmaker --help      # full command list
```

Before opening a PR, make sure `ruff check .` is clean and that
`playmaker agents` / a sample `playmaker dispatch` still work end to end.

## Pull requests

Keep PRs focused, run ruff, and describe what you changed and how you verified
it. Contributions for proper Codex/Gemini quota probes (see README) are
especially welcome.
