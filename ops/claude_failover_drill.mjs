#!/usr/bin/env node
// Failover drill for ops/claude_account_launcher.mjs (ops/FLEET.md, ops/FLEET_DESIGN.md sections 6 and 7).
// For each primary origin it runs the launcher once as a throwaway process on the shared-pool route,
// with the fallback origin configured and a stub child in place of Claude Code. The stub sends one
// count_tokens request through the launcher. One JSON line per primary reports the result and the
// host transitions the launcher logged.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

export const drillDefaults = Object.freeze({
  fallback: "http://127.0.0.1:8317",
  launcher: fileURLToPath(new URL("./claude_account_launcher.mjs", import.meta.url)),
  model: "claude-haiku-5-5",
  deadlineMs: 60_000,
});
const keyCommandPath = (home) => path.join(home, ".local/bin/cliproxy-codex-token");
const killGraceMs = 10_000;
const resultMarker = "CLIPROXY_DRILL_RESULT ";

// Stands in for Claude Code: one count_tokens request through the launcher, then exit.
const stubSource = `const started = Date.now();
let result = { status: null, host: null, inputTokens: null };
try {
  const res = await fetch(process.env.ANTHROPIC_BASE_URL + "/v1/messages/count_tokens", {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": process.env.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01" },
    body: JSON.stringify({ model: process.env.CLIPROXY_DRILL_MODEL, messages: [{ role: "user", content: "drill" }] }),
  });
  const body = await res.text();
  let inputTokens = null;
  try { inputTokens = JSON.parse(body).input_tokens ?? null; } catch {}
  result = { status: res.status, host: res.headers.get("x-cliproxy-host"), inputTokens };
} catch {
  process.exitCode = 1;
}
console.log(${JSON.stringify(resultMarker)} + JSON.stringify({ ...result, ms: Date.now() - started }));
`;

// Hides the tailnet name: will.tail1234.ts.net becomes will.<tailnet>.ts.net. The machine label stays.
export function maskHosts(text) {
  return String(text).replace(/\b(?:([a-z0-9-]+)\.)?[a-z0-9-]+\.ts\.net\b/gi, (_match, machine) =>
    machine ? `${machine}.<tailnet>.ts.net` : "<tailnet>.ts.net");
}

export function splitPrimaries(values) {
  return [values].flat().flatMap((value) => String(value ?? "").split(",")).map((value) => value.trim()).filter(Boolean);
}

// The key command's stdout is the key. Its stderr goes straight to ours; nothing it prints is echoed.
async function runKeyCommand(command, env) {
  const child = spawn(command, [], { stdio: ["ignore", "pipe", "inherit"], env });
  const chunks = [];
  child.stdout.on("data", (chunk) => chunks.push(chunk));
  let code = null;
  try {
    [code] = await once(child, "close");
  } catch {
    // A command that cannot start is reported below like one that fails.
  }
  if (code !== 0) throw new Error(`The key command failed: ${command}`);
  const key = Buffer.concat(chunks).toString("utf8").trim();
  if (!key) throw new Error(`The key command printed no key: ${command}`);
  return key;
}

// An explicit key command wins, then CLIPROXY_DRILL_KEY, then the default key command.
export async function resolveKey({ keyCommand, env = process.env } = {}) {
  if (keyCommand) return runKeyCommand(keyCommand, env);
  if (env.CLIPROXY_DRILL_KEY) return env.CLIPROXY_DRILL_KEY;
  return runKeyCommand(keyCommandPath(env.HOME || os.homedir()), env);
}

async function readTransitions(file) {
  let text;
  try {
    text = await fs.promises.readFile(file, "utf8");
  } catch {
    return [];
  }
  return text.split("\n").filter(Boolean).flatMap((line) => {
    try {
      const entry = JSON.parse(line);
      return [[entry.event, entry.reason ?? null]];
    } catch {
      return [];
    }
  });
}

function parseResult(stdout) {
  const line = stdout.split("\n").reverse().find((candidate) => candidate.startsWith(resultMarker));
  try {
    return line ? JSON.parse(line.slice(resultMarker.length)) : {};
  } catch {
    return {};
  }
}

async function runOne({ primary, fallback, launcher, model, key, env, deadlineMs, onStderr }) {
  const dir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "cliproxy-drill-"));
  try {
    const stub = path.join(dir, "stub.mjs");
    const log = path.join(dir, "failover.jsonl");
    await fs.promises.writeFile(stub, stubSource, { mode: 0o600 });
    // Only what the launcher and the stub need: the caller's other variables, CLIPROXY_DRILL_KEY
    // included, stay out of the child.
    const childEnv = {
      PATH: env.PATH ?? "", HOME: env.HOME ?? os.homedir(), DO_NOT_TRACK: "1",
      CLIPROXY_CLAUDE_ROUTE: "shared-pool", CLIPROXY_ACCOUNT_PREFIX: "",
      CLIPROXY_BASE_URL: primary, CLIPROXY_FALLBACK_URL: fallback,
      CLIPROXY_CLAUDE_BINARY: process.execPath, CLIPROXY_FAILOVER_LOG: log,
      CLIPROXY_DRILL_MODEL: model, ANTHROPIC_API_KEY: key,
    };
    const started = performance.now();
    const proc = spawn(process.execPath, [launcher, stub], { stdio: ["ignore", "pipe", "pipe"], env: childEnv });
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (chunk) => { stdout += chunk; });
    proc.stderr.on("data", (chunk) => { stderr += chunk; });
    // SIGTERM lets the launcher stop the stub; SIGKILL follows if it does not exit.
    let killTimer;
    const deadline = setTimeout(() => {
      proc.kill("SIGTERM");
      killTimer = setTimeout(() => proc.kill("SIGKILL"), killGraceMs);
    }, deadlineMs);
    const [code, signal] = await once(proc, "close");
    clearTimeout(deadline);
    clearTimeout(killTimer);
    const totalSeconds = Math.round((performance.now() - started) / 10) / 100;
    if (stderr) onStderr(maskHosts(stderr.replaceAll(key, "<key>")));
    const result = parseResult(stdout);
    return {
      primary: maskHosts(primary),
      exit: code ?? (signal ? 128 + os.constants.signals[signal] : null),
      totalSeconds,
      status: result.status ?? null,
      host: result.host ?? null,
      inputTokens: result.inputTokens ?? null,
      ms: result.ms ?? null,
      transitions: await readTransitions(log),
    };
  } finally {
    await fs.promises.rm(dir, { recursive: true, force: true });
  }
}

// Runs the primaries one after another. `ok` is true only when every run returned 200.
export async function runDrill({
  primaries,
  fallback = drillDefaults.fallback,
  launcher = drillDefaults.launcher,
  model = drillDefaults.model,
  keyCommand,
  env = process.env,
  deadlineMs = drillDefaults.deadlineMs,
  onResult = () => {},
  onStderr = (text) => process.stderr.write(text),
} = {}) {
  const list = splitPrimaries(primaries);
  if (!list.length) throw new Error("At least one --primary origin is required.");
  const key = await resolveKey({ keyCommand, env });
  const results = [];
  for (const primary of list) {
    const result = await runOne({ primary, fallback, launcher, model, key, env, deadlineMs, onStderr });
    results.push(result);
    onResult(result);
  }
  return { ok: results.every((result) => result.status === 200), results };
}

const usage = `Usage: claude_failover_drill.mjs --primary <origin>[,<origin>...] [--primary <origin>]...
  [--fallback <origin>] [--launcher <path>] [--model <id>] [--key-command <path>] [--deadline-seconds <n>]

Runs the Claude account launcher once per primary on the shared-pool route, with a stub child that
sends one count_tokens request, and prints one JSON line per primary.

  --fallback          fallback origin (default ${drillDefaults.fallback})
  --launcher          launcher script (default the sibling claude_account_launcher.mjs)
  --model             model for the count_tokens request (default ${drillDefaults.model})
  --key-command       prints the client key (default ~/.local/bin/cliproxy-codex-token)
  --deadline-seconds  stop a run that takes longer (default ${drillDefaults.deadlineMs / 1000})

Without --key-command, CLIPROXY_DRILL_KEY is used when set. The key reaches the child only as
ANTHROPIC_API_KEY and is never printed. Exits 0 only when every run returned 200.
`;

async function main() {
  const { values } = parseArgs({
    options: {
      primary: { type: "string", multiple: true },
      fallback: { type: "string" },
      launcher: { type: "string" },
      model: { type: "string" },
      "key-command": { type: "string" },
      "deadline-seconds": { type: "string" },
      help: { type: "boolean", short: "h" },
    },
  });
  if (values.help) return void process.stdout.write(usage);
  const deadlineSeconds = Number(values["deadline-seconds"] ?? drillDefaults.deadlineMs / 1000);
  if (!splitPrimaries(values.primary).length || !(deadlineSeconds > 0)) {
    process.stderr.write(usage);
    process.exitCode = 2;
    return;
  }
  const { ok } = await runDrill({
    primaries: values.primary,
    fallback: values.fallback,
    launcher: values.launcher,
    model: values.model,
    keyCommand: values["key-command"],
    deadlineMs: deadlineSeconds * 1000,
    onResult: (result) => process.stdout.write(`${JSON.stringify(result)}\n`),
  });
  process.exitCode = ok ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => { console.error(error.message); process.exitCode = 1; });
}
