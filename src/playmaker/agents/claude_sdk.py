"""The claude-api lane: Claude Code's agent through the Claude Agent SDK.

The lane spends a Console API key, never the Max subscription. It used to run
`claude --bare`, but Console bills Claude Code traffic to purchased credits
only, while the promo credit pays for Agent SDK traffic — so the lane runs
runners/claude_sdk.mjs, which calls the SDK's query() on Node and prints the
same stream-json `claude -p` prints. ClaudeHandler's parsing, resume, cost and
session-file lookup are reused as they are; this subclass changes what is
launched, the environment it gets and what has to exist before it starts.

The SDK is not bundled in the wheel (a few hundred MB with its platform
binary): `playmaker sdk install` puts a pinned copy into ~/.playmaker/sdk-runner
with npm from the lane's own Node. Until then the lane refuses at preflight.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from playmaker import __version__, state
from playmaker.agents.claude import API_KEY_ENV_VAR, ClaudeHandler, load_api_key
from playmaker.config import agent_binary, agent_setting

LANE = "claude-api"

SDK_PACKAGE = "@anthropic-ai/claude-agent-sdk"
# The version the runner is written and checked against. A different one in
# sdk_dir() refuses until `playmaker sdk install` brings it to this pin.
SDK_VERSION = "0.3.312"
SDK_DIRNAME = "sdk-runner"

# The SDK's `engines` field.
MIN_NODE = (18, 14)

RUNNER_PATH = Path(__file__).resolve().parent.parent / "runners" / "claude_sdk.mjs"

# Variables a worker must not inherit from whatever launched playmaker. A
# dispatch from inside Claude Code carries that session's entrypoint (the SDK
# sets its own `sdk-ts` only when the variable is empty, and the entrypoint is
# what Console bills by), its auth hand-off (OAuth scopes, host auth refresh,
# a bearer token), its base URL and its session ids. None of them belong to
# the lane, so every CLAUDE* and ANTHROPIC_* name is dropped and the key is
# set afresh.
_SCRUBBED_PREFIXES = ("CLAUDE", "ANTHROPIC_")

# Set on the worker: what `--bare` used to switch off, and who is calling.
_WORKER_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_AGENT_SDK_CLIENT_APP": f"playmaker/{__version__}",
}

_NODE_VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def sdk_dir() -> Path:
    """Where `playmaker sdk install` keeps the SDK: ~/.playmaker/sdk-runner."""
    return state.PLAYMAKER_HOME / SDK_DIRNAME


def installed_sdk_version() -> str | None:
    """The SDK version in sdk_dir(), or None when it is not installed."""
    manifest = sdk_dir() / "node_modules" / SDK_PACKAGE / "package.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        return None


def node_version(executable: str) -> tuple[int, int, int] | None:
    """`<executable> --version` as (major, minor, patch), or None when it is not Node."""
    try:
        proc = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _NODE_VERSION.match(proc.stdout.strip())
    if proc.returncode != 0 or match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def require_node(agent: str = LANE) -> str:
    """The lane's Node, resolved to a path; RuntimeError with the fix when unusable."""
    configured = agent_binary(agent)
    minimum = ".".join(str(part) for part in MIN_NODE)
    path = shutil.which(configured)
    if path is None:
        raise RuntimeError(
            f"{agent}: Node not found ({configured!r}) — the lane runs the Claude Agent SDK "
            f"on Node {minimum}+; install it or set [agents.{agent}] binary to its path"
        )
    version = node_version(path)
    if version is None:
        raise RuntimeError(
            f"{agent}: {configured!r} is not Node (`--version` printed no vX.Y.Z) — "
            f"[agents.{agent}] binary must point at node {minimum}+; the lane no longer "
            "runs `claude --bare`"
        )
    if version[:2] < MIN_NODE:
        found = ".".join(str(part) for part in version)
        raise RuntimeError(
            f"{agent}: Node {found} is too old for the Claude Agent SDK (needs {minimum}+) — "
            f"set [agents.{agent}] binary to a newer node"
        )
    return path


def require_sdk(agent: str = LANE) -> None:
    """RuntimeError with the fix unless the pinned SDK sits in sdk_dir()."""
    if not RUNNER_PATH.is_file():
        raise RuntimeError(
            f"{agent}: the runner is missing at {RUNNER_PATH} — reinstall playmaker"
        )
    version = installed_sdk_version()
    if version is None:
        raise RuntimeError(
            f"{agent}: the Claude Agent SDK is not installed in {sdk_dir()} — "
            "run: playmaker sdk install"
        )
    if version != SDK_VERSION:
        raise RuntimeError(
            f"{agent}: {sdk_dir()} holds {SDK_PACKAGE}@{version}, this playmaker pins "
            f"{SDK_VERSION} — run: playmaker sdk install"
        )


def max_turns(agent: str = LANE) -> int | None:
    """`[agents.<agent>] max_turns`, or None when unset. Raises on a non-positive value."""
    raw = agent_setting(agent, "max_turns")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise RuntimeError(f"{agent}: [agents.{agent}] max_turns wants a positive integer")
    return raw


def worker_env(api_key: str) -> dict[str, str]:
    """The worker's environment: inherited, minus every Claude/Anthropic variable, plus the key."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(_SCRUBBED_PREFIXES)
    }
    env.update(_WORKER_ENV)
    env[API_KEY_ENV_VAR] = api_key
    return env


def install_sdk(agent: str = LANE) -> tuple[Path, str]:
    """Install SDK_PACKAGE@SDK_VERSION into sdk_dir() with npm from the lane's Node.

    Returns (directory, installed version). Raises RuntimeError — with npm's
    last lines when npm fails — so the CLI can print it and exit.
    """
    node = require_node(agent)
    sibling = Path(node).with_name("npm")
    npm = str(sibling) if sibling.exists() else shutil.which("npm")
    if npm is None:
        raise RuntimeError(f"npm not found next to {node} or on PATH")

    target = sdk_dir()
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "playmaker-sdk-runner",
        "private": True,
        "description": "The Claude Agent SDK for playmaker's claude-api lane",
        "dependencies": {SDK_PACKAGE: SDK_VERSION},
    }
    (target / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # npm's shebang finds `node` on PATH: put the lane's own first.
    env = dict(os.environ)
    env["PATH"] = f"{Path(node).parent}{os.pathsep}{env.get('PATH', '')}"
    try:
        proc = subprocess.run(
            [npm, "install", "--no-audit", "--no-fund", "--omit=dev"],
            cwd=target,
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"npm install in {target} timed out after 15 minutes") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-15:])
        raise RuntimeError(f"npm install in {target} failed (exit {proc.returncode}):\n{tail}")

    version = installed_sdk_version()
    if version != SDK_VERSION:
        raise RuntimeError(
            f"npm install in {target} left {SDK_PACKAGE}@{version}, expected {SDK_VERSION}"
        )
    return target, version


class ClaudeSdkHandler(ClaudeHandler):
    """The claude-api lane: runners/claude_sdk.mjs on Node, the key in its environment.

    `[agents.claude-api] binary` is the Node executable (default `node`);
    `bare` no longer applies. Every other setting — permission_mode, yolo,
    allowed_tools, effort, the credit counter — reads the lane's section as
    on the `claude` lane, plus `max_turns`.
    """

    prompt_on_stdin = True

    def __init__(self, agent_key: str = LANE) -> None:
        super().__init__(agent_key)

    @property
    def bare(self) -> bool:
        return False

    def preflight(self) -> None:
        """Refuse before any session row exists: Node, the pinned SDK, max_turns, the key."""
        require_node(self.name)
        require_sdk(self.name)
        max_turns(self.name)
        load_api_key(self.name)

    def _spawn_env(self) -> dict[str, str]:
        return worker_env(load_api_key(self.name))

    def _base_cmd(self) -> list[str]:
        cmd = [agent_binary(self.name), str(RUNNER_PATH), "--sdk-dir", str(sdk_dir())]
        turns = max_turns(self.name)
        if turns is not None:
            cmd += ["--max-turns", str(turns)]
        return cmd
