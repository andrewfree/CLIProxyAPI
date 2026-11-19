#!/usr/bin/env node
const baseURLKey = "model_providers.cliproxy.base_url";
const keySegment = String.raw`([A-Za-z0-9_-]+|'[^'\r\n]*'|"(?:[^"\\\r\n]|\\.)*")`;
const providerOverride = new RegExp(String.raw`^\s*(${keySegment}\s*\.\s*${keySegment}(?:\s*\.\s*${keySegment})?)\s*=\s*`);
const quotedURL = /^(["'])([^"'\\]+)\1\s*$/;
const optionsWithValues = new Set([
  "-a", "-C", "-i", "-m", "-p", "-P", "-s", "--add-dir", "--ask-for-approval", "--cd",
  "--disable", "--enable", "--image", "--model", "--permission-profile", "--permissions-profile",
  "--profile", "--remote", "--remote-auth-token-env", "--sandbox",
]);
// An interactive prompt is also positional. Only record known command names.
const subcommands = new Set([
  "a", "agents", "app", "app-server", "apply", "archive", "cloud", "cloud-tasks", "completion",
  "debug", "delete", "doctor", "e", "exec", "exec-server", "execpolicy", "features", "fork",
  "git-credential", "help", "login", "logout", "mcp", "mcp-server", "migrate-nfs-db", "migrate-rollouts",
  "plugin", "queue", "remote-control", "responses-api-proxy", "resume", "review", "sandbox",
  "tcp-tunnel", "unarchive", "update",
]);
const shutdownSignals = ["SIGTERM", "SIGINT", "SIGHUP"];
const childShutdownGraceMs = 5000;

function inspectArgs(args) {
  const overrides = [];
  let firstPositional;
  for (let index = 0; index < args.length; index++) {
    const arg = args[index];
    if (arg === "--") break;
    if (arg === "-c" || arg === "--config") {
      const next = args[index + 1];
      const assignment = next !== undefined && !next.startsWith("-") ? args[++index] : "";
      overrides.push({ index, prefix: "", assignment });
    } else {
      const attached = /^(--config=|-c=?)(.*)$/s.exec(arg);
      if (attached) {
        const [, prefix, assignment] = attached;
        overrides.push({ index, prefix, assignment });
      } else if (optionsWithValues.has(arg)) {
        if (args[index + 1] !== undefined && !args[index + 1].startsWith("-")) index++;
      } else if (firstPositional === undefined && !arg.startsWith("-")) {
        firstPositional = arg;
      }
    }
  }
  return { overrides, subcommand: subcommands.has(firstPositional) ? firstPositional : "none" };
}

function keyName(segment) {
  if (segment === undefined) return undefined;
  if (segment.startsWith("'")) return segment.slice(1, -1);
  if (!segment.startsWith('"')) return segment;
  return segment.slice(1, -1).replace(/\\(?:u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|.)/g,
    (escape, short, long) => {
      const code = Number.parseInt(short ?? long, 16);
      return code < 128 ? String.fromCharCode(code) : escape;
    });
}

function findBaseURL(overrides) {
  let setting;
  for (const { index, prefix, assignment } of overrides) {
    const key = providerOverride.exec(assignment);
    if (!key) continue;
    const [beforeValue, rawKey, namespace, provider, fieldSegment] = key;
    if (keyName(namespace) !== "model_providers" || keyName(provider) !== "cliproxy") continue;
    const field = keyName(fieldSegment);
    if (field !== undefined && field !== "base_url") continue;
    // Codex splits the raw path on dots. Only its bare key is safe to rewrite.
    if (setting || rawKey !== baseURLKey) return undefined;
    const value = assignment.slice(beforeValue.length);
    const match = quotedURL.exec(value);
    if (!match) return undefined;
    const [, quote, url] = match;
    setting = { index, before: `${prefix}${beforeValue}${quote}`, url, after: value.slice(1 + url.length) };
  }
  return setting;
}

function parseProxyURL(value) {
  if (!/^https?:\/\//i.test(value) || /[\s\\?#"']/.test(value)) return undefined;
  try {
    const url = new URL(value);
    if (!url.username && !url.password) return url;
  } catch {
    return undefined;
  }
}

async function logDecision(subcommand, selected, reason) {
  const line = JSON.stringify({ ts: new Date().toISOString(), pid: process.pid, subcommand, selected, reason });
  try {
    const { appendFile, mkdir } = await import("node:fs/promises");
    const { dirname } = await import("node:path");
    const { fileURLToPath } = await import("node:url");
    const file = process.env.CLIPROXY_CODEX_FAILOVER_LOG ||
      fileURLToPath(new URL("./runtime/fleet/codex-failover.jsonl", import.meta.url));
    await mkdir(dirname(file), { recursive: true, mode: 0o700 });
    await appendFile(file, `${line}\n`, { mode: 0o600 });
  } catch {
    console.error("Unable to write the Codex failover decision log.");
  }
}

async function selectHost(args, { overrides, subcommand }, fallbackValue) {
  const setting = findBaseURL(overrides);
  const primary = setting && parseProxyURL(setting.url);
  const fallback = parseProxyURL(fallbackValue);
  if (!primary || !["/v1", "/v1/"].includes(primary.pathname) ||
      fallback?.pathname !== "/" || fallback.origin === primary.origin) {
    await logDecision(subcommand, "primary", "invalid_configuration");
    return;
  }

  let probeHealth;
  try {
    ({ probeHealth } = await import("./claude_account_launcher.mjs"));
  } catch {
    // A missing or broken sibling must not prevent Codex from starting.
  }
  if (typeof probeHealth !== "function") {
    await logDecision(subcommand, "primary", "invalid_configuration");
    return;
  }

  const { urlToHttpOptions } = await import("node:url");
  const controller = new AbortController();
  const [primaryHealthy, fallbackHealthy] = await Promise.all([
    probeHealth(urlToHttpOptions(primary)).then((healthy) => {
      if (healthy) controller.abort();
      return healthy;
    }),
    probeHealth(urlToHttpOptions(fallback), undefined, controller.signal),
  ]);
  if (primaryHealthy) {
    await logDecision(subcommand, "primary", "primary_healthy");
  } else if (fallbackHealthy) {
    args[setting.index] = `${setting.before}${fallback.origin}${primary.pathname}${setting.after}`;
    await logDecision(subcommand, "fallback", "fallback_healthy");
  } else {
    await logDecision(subcommand, "primary", "both_unhealthy");
  }
}

async function runCodex(binary, args) {
  const { spawn } = await import("node:child_process");
  const { constants } = await import("node:os");
  const child = spawn(binary, args, { stdio: "inherit" });
  let escalation;
  const handlers = new Map();
  for (const signal of shutdownSignals) {
    const forward = () => {
      if (escalation) return;
      child.kill(signal);
      escalation = setTimeout(() => child.kill("SIGKILL"), childShutdownGraceMs);
    };
    handlers.set(signal, forward);
    process.on(signal, forward);
  }
  const finish = (code) => {
    clearTimeout(escalation);
    for (const [signal, forward] of handlers) process.off(signal, forward);
    process.exitCode = code;
  };
  child.once("error", () => {
    console.error("Unable to launch Codex.");
    finish(1);
  });
  child.once("exit", (code, signal) => finish(code ?? (128 + constants.signals[signal])));
}

async function main() {
  const binary = process.env.CODEX_REAL_BINARY || "/opt/homebrew/bin/codex";
  const args = process.argv.slice(2);
  const fallback = process.env.CLIPROXY_FALLBACK_URL;
  const launch = fallback && inspectArgs(args);
  if (launch && launch.overrides.length) {
    await selectHost(args, launch, fallback);
  } else if (process.execve) {
    // Importing the HTTP probe can make inherited stderr nonblocking before execve.
    process.execve(binary, [binary, ...args], process.env);
  }
  await runCodex(binary, args);
}

main().catch(() => {
  console.error("Unable to launch Codex.");
  process.exitCode = 1;
});
