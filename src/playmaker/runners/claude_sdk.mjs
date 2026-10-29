// playmaker's claude-api lane: one Claude Agent SDK query() per run.
//
// The lane used to run `claude --bare` on a Console API key. Console's promo
// credit pays for Agent SDK traffic and not for Claude Code's, so the lane
// drives the same agent through the SDK instead. This runner speaks the
// `claude -p` dialect playmaker already parses:
//
//   node claude_sdk.mjs --sdk-dir DIR [--output-format stream-json|json]
//        [--model M] [--effort E] [--resume SESSION_ID] [--max-turns N]
//        [--permission-mode MODE | --dangerously-skip-permissions]
//        [--allowedTools A,B] [--disallowedTools A,B]      < prompt on stdin
//
// stream-json prints every SDK message as one JSON line (system/init first,
// with session_id; result last, with total_cost_usd, num_turns, duration_ms);
// json prints the result message alone, like `claude -p --output-format json`.
// Exit 0 on a successful result, 1 when the run failed (an API error such as
// «Credit balance is too low» goes to stderr as is, for playmaker's failure
// classifier), 2 on a usage error before any query starts.
//
// The SDK is not bundled: `playmaker sdk install` puts a pinned copy under
// ~/.playmaker/sdk-runner, which --sdk-dir names. The environment arrives
// prepared by playmaker (ANTHROPIC_API_KEY, no inherited Claude Code session
// variables); this file adds nothing to it.

import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const SDK_PACKAGE = "@anthropic-ai/claude-agent-sdk";

// The only credential the lane may spend. Anything else in system/init — "none"
// is a claude.ai OAuth login — would bill the subscription the lane exists to spare.
const REQUIRED_API_KEY_SOURCE = "ANTHROPIC_API_KEY";

// The child's stderr is kept for a run that dies without a result message.
const STDERR_TAIL_CHARS = 2000;

function usage(message) {
  process.stderr.write(`claude_sdk.mjs: ${message}\n`);
  process.exit(2);
}

function parseCli(argv) {
  try {
    return parseArgs({
      args: argv,
      options: {
        "sdk-dir": { type: "string" },
        "output-format": { type: "string", default: "stream-json" },
        // `claude -p` wants --verbose with stream-json; accepted and ignored here.
        verbose: { type: "boolean" },
        model: { type: "string" },
        effort: { type: "string" },
        resume: { type: "string" },
        "max-turns": { type: "string" },
        "permission-mode": { type: "string" },
        "dangerously-skip-permissions": { type: "boolean" },
        allowedTools: { type: "string" },
        disallowedTools: { type: "string" },
      },
      strict: true,
      allowPositionals: false,
    }).values;
  } catch (error) {
    usage(error.message);
  }
}

function toolList(value) {
  return value
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);
}

function buildOptions(values, abortController, stderrTail) {
  const options = {
    cwd: process.cwd(),
    abortController,
    // What `claude --bare` gave the lane: Claude Code's own prompt and tools,
    // no settings files (so no hooks and no CLAUDE.md discovery) and no MCP
    // servers from any config. The prompt carries its own context.
    systemPrompt: { type: "preset", preset: "claude_code" },
    tools: { type: "preset", preset: "claude_code" },
    settingSources: [],
    strictMcpConfig: true,
    stderr: (chunk) => {
      stderrTail.text = (stderrTail.text + chunk).slice(-STDERR_TAIL_CHARS);
    },
  };
  if (values.model) options.model = values.model;
  if (values.effort) options.effort = values.effort;
  if (values.resume) options.resume = values.resume;
  if (values["max-turns"]) {
    const turns = Number.parseInt(values["max-turns"], 10);
    if (!Number.isInteger(turns) || turns < 1) usage(`--max-turns wants a positive integer`);
    options.maxTurns = turns;
  }
  if (values["dangerously-skip-permissions"]) {
    options.permissionMode = "bypassPermissions";
    options.allowDangerouslySkipPermissions = true;
  } else if (values["permission-mode"]) {
    options.permissionMode = values["permission-mode"];
  }
  if (values.allowedTools) options.allowedTools = toolList(values.allowedTools);
  if (values.disallowedTools) options.disallowedTools = toolList(values.disallowedTools);
  return options;
}

async function loadQuery(sdkDir) {
  let entry;
  try {
    entry = createRequire(join(sdkDir, "package.json")).resolve(SDK_PACKAGE);
  } catch {
    usage(`${SDK_PACKAGE} is not installed in ${sdkDir} — run: playmaker sdk install`);
  }
  const sdk = await import(pathToFileURL(entry).href);
  return sdk.query;
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

function resultFailure(result) {
  if (result.subtype === "success" && !result.is_error) return null;
  const errors = Array.isArray(result.errors) ? result.errors.filter(Boolean) : [];
  return (typeof result.result === "string" && result.result) || errors.join("; ") || result.subtype;
}

async function main() {
  const values = parseCli(process.argv.slice(2));
  const format = values["output-format"];
  if (format !== "stream-json" && format !== "json") {
    usage(`--output-format wants stream-json or json, not ${format}`);
  }
  if (!values["sdk-dir"]) usage("--sdk-dir is required");

  const query = await loadQuery(values["sdk-dir"]);
  const prompt = await readStdin();
  if (!prompt.trim()) usage("no prompt on stdin");

  const abortController = new AbortController();
  for (const signal of ["SIGTERM", "SIGINT"]) {
    process.on(signal, () => abortController.abort());
  }
  const stderrTail = { text: "" };
  const options = buildOptions(values, abortController, stderrTail);

  let result = null;
  let refusal = null;
  try {
    for await (const message of query({ prompt, options })) {
      if (format === "stream-json") process.stdout.write(`${JSON.stringify(message)}\n`);
      if (message.type === "system" && message.subtype === "init") {
        if (message.apiKeySource !== REQUIRED_API_KEY_SOURCE) {
          refusal =
            `the Agent SDK authenticated with ${JSON.stringify(message.apiKeySource)}, ` +
            `not ${REQUIRED_API_KEY_SOURCE}; stopped before the subscription is spent`;
          abortController.abort();
          break;
        }
      } else if (message.type === "result") {
        result = message;
      }
    }
  } catch (error) {
    // query() throws after yielding an error result ("Claude Code returned an
    // error result: …"); the result already says why. Without one, the throw
    // and the child's stderr are all there is.
    if (!result && !refusal) {
      const detail = [String(error?.message ?? error), stderrTail.text.trim()]
        .filter(Boolean)
        .join("\n");
      process.stderr.write(`${detail}\n`);
      return 1;
    }
  }

  if (refusal) {
    process.stderr.write(`${refusal}\n`);
    return 1;
  }
  if (!result) {
    process.stderr.write(`the Agent SDK ended without a result message\n${stderrTail.text}`);
    return 1;
  }
  if (format === "json") process.stdout.write(`${JSON.stringify(result)}\n`);
  const failure = resultFailure(result);
  if (failure) {
    process.stderr.write(`${failure}\n`);
    return 1;
  }
  return 0;
}

process.exitCode = await main();
