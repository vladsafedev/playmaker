# Security

## Reporting a vulnerability

Report privately through GitHub's
[security advisory form](https://github.com/vladsafedev/playmaker/security/advisories/new)
rather than opening a public issue. Expect a first response within a week.

## What this tool touches

`playmaker` is a local orchestrator: it spawns other CLIs as subprocesses on
your machine. It has no server, no telemetry, and makes no network calls other
than the quota probes described below.

### Credentials

The quota probes read tokens that the agent CLIs have already stored, and send
each token only to that vendor's own endpoint:

| Source | Read from | Sent to |
|---|---|---|
| Claude | macOS Keychain entry `Claude Code-credentials` | `api.anthropic.com` |
| Codex | `~/.codex/auth.json` | `chatgpt.com` |
| Antigravity | agy's localhost daemon, else `~/.gemini/oauth_creds.json` | `127.0.0.1`, else `daily-cloudcode-pa.googleapis.com` |
| Z.ai | `~/.local/share/opencode/auth.json`, else `$ZAI_API_KEY` | `api.z.ai` |
| Kimi Code | `~/.kimi-code/credentials/kimi-code-env-*.json` (OAuth token written by the Kimi Code CLI) | `api.kimi.ai`, `auth.kimi.ai` (refresh) |

Tokens are held in memory for the duration of a probe and are never written to
disk by playmaker, except that Kimi Code refreshes an expired OAuth access
token in the CLI's existing credential file with mode `0600`. `~/.playmaker/quotas.json`
holds the probe results — remaining percentages, reset times, and the account
email and plan tier the provider reported.

The OAuth client id/secret literals in `quotas.py` are the ones published in
the open-source gemini-cli package. They are installed-app credentials, which
are not confidential per Google's own OAuth documentation. They are written as
concatenated fragments only so that GitHub's secret scanner does not flag every
fork.

The localhost Antigravity probe disables TLS verification because agy's
embedded daemon serves a self-signed certificate on `127.0.0.1`. The connection
never leaves the loopback interface.

### What a dispatched agent is allowed to do

A detached agent cannot answer a permission prompt, so the answer is given
ahead of time in `~/.playmaker/config.toml`. Defaults as of 0.5:

| Agent | Default | Boundary |
|---|---|---|
| claude | `permission_mode = "acceptEdits"` | free inside the dispatch `--cwd`; claude refuses writes outside it |
| codex | no flag passed | codex's own sandbox for model-run shell commands |
| agy | `yolo = true` | none — agy exposes no per-mode permission flag |
| opencode | `yolo = true` → `--auto` | whatever `permission` says in your own opencode.json; `--auto` approves only what is not explicitly denied |
| gemini | `--yolo` | none |

Setting `yolo = true` (or the legacy `skip_permissions = true`) removes the
boundary for that agent, including the working-directory one. It is a
supported choice, not a trap — but it should be a choice.

Either way, **a dispatch is as dangerous as the prompt you give it**. Treat
`playmaker dispatch` like running an untrusted script in that directory.

Prompt content is the real attack surface: an agent that reads a file
containing instructions may act on them, and the working-directory boundary
does not help against a prompt that tells the agent to do something harmful
inside that directory. Don't dispatch against directories whose contents you
don't trust.

### Data at rest

`~/.playmaker/` stores your prompts, each agent's final output
(`outputs/<id>.md`) and subprocess logs (`logs/<id>.log`) unencrypted, under
your user account's default permissions. These files can contain source code
and anything else the agent printed. `playmaker` never deletes them — prune the
directory yourself if that matters to you.

## Supported versions

Fixes land on `main` and in the next release. Only the latest release is
supported.
