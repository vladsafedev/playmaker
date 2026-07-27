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

Tokens are held in memory for the duration of a probe and are never written to
disk by playmaker. `~/.playmaker/quotas.json` holds the probe results —
remaining percentages, reset times, and the account email and plan tier the
provider reported.

The OAuth client id/secret literals in `quotas.py` are the ones published in
the open-source gemini-cli package. They are installed-app credentials, which
are not confidential per Google's own OAuth documentation. They are written as
concatenated fragments only so that GitHub's secret scanner does not flag every
fork.

The localhost Antigravity probe disables TLS verification because agy's
embedded daemon serves a self-signed certificate on `127.0.0.1`. The connection
never leaves the loopback interface.

### Sub-agents run without permission prompts

By default playmaker passes `--dangerously-skip-permissions` to Claude and
Antigravity (and `--yolo` to Gemini). A dispatched agent can therefore run
commands and edit files in the working directory you point it at, without
asking. This is deliberate — a detached run has no human to approve prompts —
but it means **a dispatch is as dangerous as the prompt you give it**. Treat
`playmaker dispatch` like running an untrusted script in that directory.

Prompt content is the real attack surface here: an agent that reads a file
containing instructions may act on them. Don't dispatch against directories
whose contents you don't trust.

To restore Claude's permission checks:

```toml
[agents.claude]
skip_permissions = false
```

### Data at rest

`~/.playmaker/` stores your prompts, each agent's final output
(`outputs/<id>.md`) and subprocess logs (`logs/<id>.log`) unencrypted, under
your user account's default permissions. These files can contain source code
and anything else the agent printed. `playmaker` never deletes them — prune the
directory yourself if that matters to you.

## Supported versions

Fixes land on `main` and in the next release. Only the latest release is
supported.
