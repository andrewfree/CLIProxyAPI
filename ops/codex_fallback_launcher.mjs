#!/usr/bin/env node
import { spawn } from "node:child_process";
import { appendFile, mkdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, urlToHttpOptions } from "node:url";
import { probeHealth } from "./claude_account_launcher.mjs";

const defaultLog = fileURLToPath(new URL("./runtime/fleet/codex-failover.jsonl", import.meta.url));
const baseURLKey = /^\s*model_providers\.cliproxy\.base_url\s*=/;
const baseURLSetting = /^(\s*model_providers\.cliproxy\.base_url\s*=\s*")([^"\\]+)("\s*)$/;
const shutdownSignals = ["SIGTERM", "SIGINT", "SIGHUP"];
const childShutdownGraceMs = 5000;

function findBaseURL(args) {
  let setting;
  for (let index = 0; index < args.length - 1; index++) {
    if (args[index] === "--") break;
    if (args[index] !== "-c" && args[index] !== "--config") continue;
    const value = args[++index];
    if (!baseURLKey.test(value)) continue;
    const match = baseURLSetting.exec(value);
    if (setting || !match) return undefined;
    const [, before, url, after] = match;
    setting = { index, before, url, after };
  }
  return setting;
}

function parseProxyURL(value) {
  if (!/^https?:\/\//i.test(value) || /[\s\\?#]/.test(value)) return undefined;
  try {
    const url = new URL(value);
    if (!url.username && !url.password) return url;
  } catch {
    return undefined;
  }
}

async function logDecision(selected, reason) {
  const file = process.env.CLIPROXY_CODEX_FAILOVER_LOG || defaultLog;
  const line = JSON.stringify({ ts: new Date().toISOString(), pid: process.pid, selected, reason });
  try {
    await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    await appendFile(file, `${line}\n`, { mode: 0o600 });
  } catch {
    console.error("Unable to write the Codex failover decision log.");
  }
}

async function selectHost(args, fallbackValue) {
  const setting = findBaseURL(args);
  const primary = setting && parseProxyURL(setting.url);
  const fallback = parseProxyURL(fallbackValue);
  if (!primary || !["/v1", "/v1/"].includes(primary.pathname) ||
      fallback?.pathname !== "/" || fallback.origin === primary.origin) {
    await logDecision("primary", "invalid_configuration");
    return;
  }

  if (await probeHealth(urlToHttpOptions(primary))) {
    await logDecision("primary", "primary_healthy");
  } else if (await probeHealth(urlToHttpOptions(fallback))) {
    args[setting.index] = `${setting.before}${fallback.origin}${primary.pathname}${setting.after}`;
    await logDecision("fallback", "fallback_healthy");
  } else {
    await logDecision("primary", "both_unhealthy");
  }
}

function runCodex(binary, args) {
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
  child.once("exit", (code, signal) => finish(code ?? (128 + os.constants.signals[signal])));
}

async function main() {
  const binary = process.env.CODEX_REAL_BINARY || "/opt/homebrew/bin/codex";
  const args = process.argv.slice(2);
  const fallback = process.env.CLIPROXY_FALLBACK_URL;
  if (fallback) {
    await selectHost(args, fallback);
  } else if (process.execve) {
    process.execve(binary, [binary, ...args], process.env);
  }
  runCodex(binary, args);
}

main().catch(() => {
  console.error("Unable to launch Codex.");
  process.exitCode = 1;
});
