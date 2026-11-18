import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import { fallbackRequest, poolFallbackEnvironment, savedReport } from "./set_pool_fallback.mjs";
import { poolProviders } from "./configure_claude_pool.mjs";

const tool = fileURLToPath(new URL("./set_pool_fallback.mjs", import.meta.url));
const primary = "http://will.example:8317";
const fallback = "http://127.0.0.1:8317";
const apiKey = "fixture-key";

// A pool profile environment as server.getConfig returns it, with the key redacted.
const saved = [
  { name: "CLIPROXY_CLAUDE_ROUTE", value: "shared-pool", sensitive: false },
  { name: "CLIPROXY_ACCOUNT_PREFIX", value: "", sensitive: false },
  { name: "CLIPROXY_BASE_URL", value: "http://127.0.0.1:18317", sensitive: false },
  { name: "CLIPROXY_CLAUDE_BINARY", value: "/bin/claude", sensitive: false },
  { name: "ANTHROPIC_API_KEY", value: "", sensitive: true, valueRedacted: true },
  { name: "ANTHROPIC_AUTH_TOKEN", value: "", sensitive: false },
  { name: "CLAUDE_CODE_OAUTH_TOKEN", value: "", sensitive: false },
  { name: "DO_NOT_TRACK", value: "1", sensitive: false },
  { name: "CLIPROXY_CONNECT_TIMEOUT_MS", value: "3000", sensitive: false },
];
const names = (environment) => environment.map((variable) => variable.name);
const value = (environment, name) => environment.find((variable) => variable.name === name)?.value;

test("setting a fallback puts it right after the primary and keeps every other variable in order", () => {
  const before = structuredClone(saved);
  const result = poolFallbackEnvironment(saved, { primary: `${primary}/`, fallback: `${fallback}/`, apiKey: ` ${apiKey}\n` });
  assert.deepEqual(saved, before);
  const expected = names(saved);
  expected.splice(expected.indexOf("CLIPROXY_BASE_URL") + 1, 0, "CLIPROXY_FALLBACK_URL");
  assert.deepEqual(names(result), expected);
  assert.deepEqual(result.find((v) => v.name === "CLIPROXY_BASE_URL"), { name: "CLIPROXY_BASE_URL", value: primary, sensitive: false });
  assert.deepEqual(result.find((v) => v.name === "CLIPROXY_FALLBACK_URL"), { name: "CLIPROXY_FALLBACK_URL", value: fallback, sensitive: false });
  assert.deepEqual(result.find((v) => v.name === "ANTHROPIC_API_KEY"), { name: "ANTHROPIC_API_KEY", value: apiKey, sensitive: true });
  for (const name of ["CLIPROXY_CLAUDE_BINARY", "DO_NOT_TRACK", "CLIPROXY_CONNECT_TIMEOUT_MS"]) {
    assert.deepEqual(result.find((v) => v.name === name), saved.find((v) => v.name === name));
  }
});

test("a new fallback replaces the old one wherever it was, and reapplying changes nothing", () => {
  const misplaced = [...saved, { name: "CLIPROXY_FALLBACK_URL", value: "http://old.example:8317", sensitive: false }];
  const result = poolFallbackEnvironment(misplaced, { primary, fallback, apiKey });
  assert.equal(names(result).filter((name) => name === "CLIPROXY_FALLBACK_URL").length, 1);
  assert.equal(names(result).indexOf("CLIPROXY_FALLBACK_URL"), names(result).indexOf("CLIPROXY_BASE_URL") + 1);
  assert.equal(value(result, "CLIPROXY_FALLBACK_URL"), fallback);
  assert.deepEqual(poolFallbackEnvironment(result, { primary, fallback, apiKey }), result);
});

test("a null fallback removes it and leaves the rest in order", () => {
  const withFallback = poolFallbackEnvironment(saved, { primary, fallback, apiKey });
  const result = poolFallbackEnvironment(withFallback, { primary, fallback: null, apiKey });
  assert.deepEqual(names(result), names(saved));
  assert.equal(value(result, "CLIPROXY_BASE_URL"), primary);
  assert.equal(value(result, "ANTHROPIC_API_KEY"), apiKey);
});

test("the result matches the layout the pool configurator writes", () => {
  const providers = { claudeAgent: { driver: "claudeAgent", config: { binaryPath: "/bin/claude" } } };
  const options = { id: "claude_cliproxy", label: "Pool", upstream: primary, launcher: "/repo/ops/claude_account_launcher.mjs", binary: "/bin/claude", apiKey };
  const plain = poolProviders(providers, options).claude_cliproxy.environment;
  const configured = poolProviders(providers, { ...options, fallback }).claude_cliproxy.environment;
  assert.deepEqual(poolFallbackEnvironment(plain, { primary, fallback, apiKey }), configured);
  assert.deepEqual(poolFallbackEnvironment(configured, { primary, fallback: null, apiKey }), plain);
});

test("refuses profiles that are not shared pools or cannot keep their key", () => {
  const options = { primary, fallback, apiKey };
  const named = saved.filter((v) => v.name !== "CLIPROXY_CLAUDE_ROUTE").map((v) => (v.name === "CLIPROXY_ACCOUNT_PREFIX" ? { ...v, value: "t3-pro" } : v));
  assert.throws(() => poolFallbackEnvironment(named, options), /not a shared Claude pool/);
  assert.throws(() => poolFallbackEnvironment(saved.map((v) => (v.name === "CLIPROXY_CLAUDE_ROUTE" ? { ...v, value: "named" } : v)), options), /not a shared Claude pool/);
  assert.throws(() => poolFallbackEnvironment([], options), /not a shared Claude pool/);
  assert.throws(() => poolFallbackEnvironment(saved.filter((v) => v.name !== "ANTHROPIC_API_KEY"), options), /without ANTHROPIC_API_KEY/);
  assert.throws(() => poolFallbackEnvironment(saved.filter((v) => v.name !== "CLIPROXY_BASE_URL"), options), /without CLIPROXY_BASE_URL/);
  for (const key of ["", "  \n", undefined]) assert.throws(() => poolFallbackEnvironment(saved, { ...options, apiKey: key }), /API key is required/);
});

test("refuses a key with inner whitespace or characters outside the token set", () => {
  const options = { primary, fallback };
  for (const key of ["two words", "tab\tkey", "line\nkey", "key$", "key;rm", "ключ", "key\u0000"]) {
    assert.throws(() => fallbackRequest({ ...options, apiKey: key }), /may hold only letters, digits/);
    assert.throws(() => poolFallbackEnvironment(saved, { ...options, apiKey: key }), (error) => !error.message.includes(key));
  }
  assert.equal(fallbackRequest({ ...options, apiKey: " sk-A.b_c~d+e/f=g-0\r\n" }).apiKey, "sk-A.b_c~d+e/f=g-0");
});

test("refuses a profile with more than one base or fallback URL", () => {
  const options = { primary, fallback, apiKey };
  const twice = (name, value) => [...saved, { name, value, sensitive: false }];
  assert.throws(() => poolFallbackEnvironment(twice("CLIPROXY_BASE_URL", "http://other.example:8317"), options), /more than one CLIPROXY_BASE_URL/);
  const withFallback = poolFallbackEnvironment(saved, options);
  assert.throws(() => poolFallbackEnvironment([...withFallback, { name: "CLIPROXY_FALLBACK_URL", value: fallback, sensitive: false }], options), /more than one CLIPROXY_FALLBACK_URL/);
});

// The redaction T3 applies to server.getConfig.
const redacted = (environment) => environment.map((v) => (v.sensitive ? { ...v, value: "", valueRedacted: true } : v));

test("the saved report prints only the route and host URLs, masked, and checks the profile's identity", () => {
  const profile = {
    driver: "claudeAgent", displayName: "Pool", enabled: true, config: { binaryPath: "/repo/ops/claude_account_launcher.mjs" },
    environment: [...saved, { name: "EXTRA_SECRET", value: "", sensitive: true, valueRedacted: true }],
  };
  const before = { claudeAgent: { driver: "claudeAgent", displayName: "Direct", enabled: true }, claude_cliproxy: profile };
  const environment = poolFallbackEnvironment(profile.environment, { primary: "http://will.tailabc123.ts.net:8317", fallback, apiKey });
  const after = (change = {}) => ({ ...before, claude_cliproxy: { ...profile, environment: redacted(environment), ...change } });

  const { report, ok } = savedReport({ id: "claude_cliproxy", before, after: after(), environment });
  assert.equal(ok, true);
  assert.deepEqual(report.environment, {
    CLIPROXY_CLAUDE_ROUTE: "shared-pool", CLIPROXY_BASE_URL: "http://will.<tailnet>.ts.net:8317", CLIPROXY_FALLBACK_URL: fallback,
  });
  assert.equal(report.profileUnchanged, true);

  const unflagged = environment.map((v) => (v.name === "ANTHROPIC_API_KEY" ? { ...v, sensitive: false } : v));
  const leaked = savedReport({ id: "claude_cliproxy", before, after: after({ environment: redacted(unflagged) }), environment });
  assert.equal(leaked.ok, false);
  assert.doesNotMatch(JSON.stringify(leaked.report), /fixture-key|tailabc123/);

  for (const change of [
    { displayName: "Renamed" },
    { enabled: false },
    { environment: redacted(environment.filter((v) => v.name !== "EXTRA_SECRET")) },
    { environment: redacted(environment.map((v) => (v.name === "EXTRA_SECRET" ? { ...v, name: "OTHER_SECRET" } : v))) },
  ]) {
    const result = savedReport({ id: "claude_cliproxy", before, after: after(change), environment });
    assert.equal(result.ok, false, JSON.stringify(change));
    assert.equal(result.report.profileUnchanged, false, JSON.stringify(change));
  }
});

test("refuses a fallback equal to the primary, ambiguous origins, and an unstated fallback", () => {
  const options = { primary, fallback, apiKey };
  assert.throws(() => poolFallbackEnvironment(saved, { ...options, fallback: `${primary}/` }), /different CLIProxy host/);
  for (const bad of ["http://user:secret@localhost", "http://localhost/v1", "http://localhost?key=secret", "http://localhost/#x", "file:///host", "not a url"]) {
    assert.throws(() => poolFallbackEnvironment(saved, { ...options, primary: bad }), /primary must be an HTTP origin/);
    assert.throws(() => poolFallbackEnvironment(saved, { ...options, fallback: bad }), /fallback must be an HTTP origin/);
  }
  assert.throws(() => poolFallbackEnvironment(saved, { primary, apiKey }), /null to remove it/);
});

test("the CLI rejects bad arguments and prints no key when T3 is unreachable", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "set-pool-fallback-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const backupDir = path.join(dir, "backup");
  const args = ["--base-dir", path.join(dir, "t3"), "--profile-id", "claude_cliproxy", "--primary", primary, "--backup-dir", backupDir];
  const run = (extra, input = apiKey) => spawnSync(process.execPath, [tool, ...extra], { input, encoding: "utf8" });

  const missing = run(["--profile-id", "claude_cliproxy"]);
  assert.equal(missing.status, 1);
  assert.match(missing.stderr, /--base-dir is required/);
  assert.match(missing.stderr, /--no-key-check skips that check/);
  const both = run([...args, "--fallback", fallback, "--no-fallback"]);
  assert.equal(both.status, 1);
  assert.match(both.stderr, /exactly one of --fallback/);
  const stray = run([...args, "--no-fallback", "stray-secret-value"]);
  assert.equal(stray.status, 1);
  assert.match(stray.stderr, /^Usage:/);
  assert.doesNotMatch(stray.stderr, /stray-secret-value/);

  const unreachable = run([...args, "--fallback", fallback, "--no-key-check"]);
  assert.equal(unreachable.status, 1);
  assert.match(unreachable.stderr, /no secret values are printed/);
  assert.doesNotMatch(unreachable.stdout + unreachable.stderr, /fixture-key/);
  assert.deepEqual(fs.readdirSync(backupDir), []);
});

function tempDir(t, prefix) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

async function listen(t, server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => { server.closeAllConnections?.(); server.close(); });
  return `http://127.0.0.1:${server.address().port}`;
}

// A CLIProxy stand-in whose GET /v1/models returns 200 only for `key`.
async function modelsServer(t, key = apiKey) {
  const requests = [];
  const server = http.createServer((request, response) => {
    requests.push([request.method, request.url, request.headers.authorization]);
    const accepted = request.method === "GET" && request.url === "/v1/models" && request.headers.authorization === `Bearer ${key}`;
    response.writeHead(accepted ? 200 : 401, { "content-type": "application/json" });
    response.end(accepted ? '{"object":"list","data":[]}' : '{"error":"unauthorized"}');
  });
  return { origin: await listen(t, server), requests };
}

// Holds its port and resets every connection, like a host that is down.
async function downOrigin(t) {
  return listen(t, net.createServer((socket) => socket.resetAndDestroy()));
}

const fakeUndici = (stateFile) => `const fs = require("node:fs");
const stateFile = ${JSON.stringify(stateFile)};
const redact = (settings) => ({
  ...settings,
  providerInstances: Object.fromEntries(Object.entries(settings.providerInstances).map(([id, instance]) => [id, instance.environment
    ? { ...instance, environment: instance.environment.map((v) => (v.sensitive ? { ...v, value: "", valueRedacted: true } : v)) }
    : instance])),
});
class WebSocket {
  constructor() { this.listeners = {}; setImmediate(() => this.emit("open", {})); }
  addEventListener(type, listener) { (this.listeners[type] ??= []).push(listener); }
  emit(type, event) { for (const listener of this.listeners[type] ?? []) listener(event); }
  send(text) {
    const message = JSON.parse(text);
    if (message._tag !== "Request") return;
    const state = JSON.parse(fs.readFileSync(stateFile, "utf8"));
    let value = {};
    if (message.tag === "server.getConfig") value = { settings: redact(state.settings) };
    if (message.tag === "server.updateSettings") {
      state.settings = { ...state.settings, ...message.payload.patch };
      for (const instance of Object.values(state.settings.providerInstances)) {
        for (const variable of instance.environment ?? []) if (state.unflagKey && variable.name === "ANTHROPIC_API_KEY") variable.sensitive = false;
      }
      fs.writeFileSync(stateFile, JSON.stringify(state));
    }
    setImmediate(() => this.emit("message", { data: JSON.stringify({ _tag: "Exit", requestId: message.id, exit: { _tag: "Success", value } }) }));
  }
  close() {}
}
module.exports = { WebSocket };
`;

// A T3 base directory with a fake t3 CLI that logs session issue and revoke calls, and a fake undici
// WebSocket that serves server.getConfig and server.updateSettings from state.json, redacting
// sensitive values as T3 does. `unflagKey` saves ANTHROPIC_API_KEY without its sensitive flag.
function fakeT3(t, { unflagKey = false, revokeFails = false } = {}) {
  const base = tempDir(t, "set-pool-fallback-t3-");
  const version = "0.0.0-test";
  const versionDir = path.join(base, "runtime/versions", version);
  fs.mkdirSync(path.join(versionDir, "node_modules/undici"), { recursive: true });
  fs.mkdirSync(path.join(base, "userdata"));
  fs.writeFileSync(path.join(base, "userdata/server-runtime.json"), JSON.stringify({ serverVersion: version, port: 9 }));
  const calls = path.join(base, "calls.log");
  fs.writeFileSync(path.join(versionDir, "t3"), `#!/bin/sh
printf '%s\\n' "$3" >> '${calls}'
if [ "$3" = issue ]; then printf '{"sessionId":"fake-session","token":"fake-token"}\\n'; exit 0; fi
exit ${revokeFails ? 1 : 0}
`, { mode: 0o700 });
  const stateFile = path.join(base, "state.json");
  fs.writeFileSync(path.join(versionDir, "node_modules/undici/index.js"), fakeUndici(stateFile));
  const direct = { driver: "claudeAgent", displayName: "Direct", enabled: true, config: { binaryPath: "/bin/claude" } };
  const providers = poolProviders({ claudeAgent: direct }, {
    id: "claude_cliproxy", label: "Pool", upstream: "http://127.0.0.1:18317", launcher: "/repo/ops/claude_account_launcher.mjs", binary: "/bin/claude", apiKey: "old-key",
  });
  fs.writeFileSync(stateFile, JSON.stringify({ unflagKey, settings: { providerInstances: providers } }));
  return {
    base,
    calls: () => (fs.existsSync(calls) ? fs.readFileSync(calls, "utf8").split("\n").filter(Boolean) : []),
    pool: () => JSON.parse(fs.readFileSync(stateFile, "utf8")).settings.providerInstances.claude_cliproxy,
  };
}

// Async, so the in-process fake servers can answer while the tool runs.
async function runTool(args, input = ` ${apiKey}\n`) {
  const proc = spawn(process.execPath, [tool, ...args], { stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  proc.stdout.on("data", (chunk) => { stdout += chunk; });
  proc.stderr.on("data", (chunk) => { stderr += chunk; });
  proc.stdin.end(input);
  const [code] = await once(proc, "close");
  return { code, stdout, stderr };
}

function toolArgs(t3, backupDir, ...extra) {
  return ["--base-dir", t3.base, "--profile-id", "claude_cliproxy", "--backup-dir", backupDir, ...extra];
}

test("the CLI checks the key on both hosts, saves the fallback, and reports only the route and host URLs", async (t) => {
  const [primaryHost, fallbackHost] = [await modelsServer(t), await modelsServer(t)];
  const t3 = fakeT3(t);
  const backupDir = path.join(tempDir(t, "set-pool-fallback-backup-"), "backup");
  const { code, stdout, stderr } = await runTool(toolArgs(t3, backupDir, "--primary", primaryHost.origin, "--fallback", fallbackHost.origin));
  assert.equal(code, 0, stderr);
  const report = JSON.parse(stdout);
  assert.deepEqual(report, {
    profile: "claude_cliproxy",
    environment: { CLIPROXY_CLAUDE_ROUTE: "shared-pool", CLIPROXY_BASE_URL: primaryHost.origin, CLIPROXY_FALLBACK_URL: fallbackHost.origin },
    apiKey: { sensitive: true, redacted: true, valueVisible: false },
    environmentApplied: true, profileUnchanged: true, configUnchanged: true, otherProfilesUnchanged: true,
  });
  for (const host of [primaryHost, fallbackHost]) assert.deepEqual(host.requests, [["GET", "/v1/models", `Bearer ${apiKey}`]]);
  assert.deepEqual(t3.calls(), ["issue", "revoke"]);
  const stored = t3.pool().environment;
  assert.deepEqual(stored.find((v) => v.name === "ANTHROPIC_API_KEY"), { name: "ANTHROPIC_API_KEY", value: apiKey, sensitive: true });
  assert.equal(value(stored, "CLIPROXY_FALLBACK_URL"), fallbackHost.origin);
  const backups = fs.readdirSync(backupDir);
  assert.equal(backups.length, 2);
  for (const file of backups) assert.equal(fs.statSync(path.join(backupDir, file)).mode & 0o777, 0o600);
  assert.doesNotMatch(stdout + stderr, /fixture-key/);
});

test("the CLI never prints a key T3 saved without its sensitive flag, or a tailnet name", async (t) => {
  const t3 = fakeT3(t, { unflagKey: true });
  const backupDir = path.join(tempDir(t, "set-pool-fallback-backup-"), "backup");
  // --no-key-check keeps the ts.net name from reaching DNS.
  const { code, stdout, stderr } = await runTool(toolArgs(t3, backupDir, "--primary", "http://will.tailabc123.ts.net:8317", "--no-fallback", "--no-key-check"));
  assert.equal(code, 1);
  assert.match(stderr, /did not verify\. Restore the profile from the private before backup/);
  const report = JSON.parse(stdout);
  assert.deepEqual(report.environment, { CLIPROXY_CLAUDE_ROUTE: "shared-pool", CLIPROXY_BASE_URL: "http://will.<tailnet>.ts.net:8317" });
  assert.equal(report.apiKey.sensitive, false);
  assert.doesNotMatch(stdout + stderr, /fixture-key|tailabc123/);
});

test("a failed session revoke keeps the verification result", async (t) => {
  const backupDir = () => path.join(tempDir(t, "set-pool-fallback-backup-"), "backup");
  const unverified = fakeT3(t, { unflagKey: true, revokeFails: true });
  const failed = await runTool(toolArgs(unverified, backupDir(), "--primary", primary, "--no-fallback", "--no-key-check"));
  assert.equal(failed.code, 1);
  assert.match(failed.stderr, /did not verify\. Restore the profile from the private before backup/);
  assert.deepEqual(unverified.calls(), ["issue", "revoke"]);

  const verified = fakeT3(t, { revokeFails: true });
  const saved = await runTool(toolArgs(verified, backupDir(), "--primary", primary, "--no-fallback", "--no-key-check"));
  assert.equal(saved.code, 1);
  assert.equal(JSON.parse(saved.stdout).environmentApplied, true);
  assert.match(saved.stderr, /saved and verified, but the temporary T3 session could not be revoked/);
});

test("the key, the origins and the arguments are checked before a T3 session is issued", async (t) => {
  const accepting = await modelsServer(t);
  const rejecting = await modelsServer(t, "another-key");
  const down = await downOrigin(t);
  const t3 = fakeT3(t);
  const backupDir = path.join(tempDir(t, "set-pool-fallback-backup-"), "backup");
  const run = (...extra) => runTool(toolArgs(t3, backupDir, ...extra));
  const refused = async (result, pattern) => {
    assert.equal(result.code, 1);
    assert.match(result.stderr, pattern);
    assert.doesNotMatch(result.stdout + result.stderr, /fixture-key/);
    assert.deepEqual(t3.calls(), []);
    assert.equal(fs.existsSync(backupDir), false);
  };

  await refused(await run("--primary", rejecting.origin, "--no-fallback"), /key check on the primary http:\/\/127\.0\.0\.1:\d+ failed: GET \/v1\/models returned 401/);
  assert.deepEqual(rejecting.requests, [["GET", "/v1/models", `Bearer ${apiKey}`]]);
  await refused(await run("--primary", accepting.origin, "--fallback", rejecting.origin), /key check on the fallback .* returned 401/);
  await refused(await run("--primary", accepting.origin, "--fallback", down), /key check on the fallback .* returned no response, not 200.*--no-key-check/);
  const checked = accepting.requests.length;
  for (const key of ["fixture key", "fixture\tkey", "fixture$key"]) {
    await refused(await runTool(toolArgs(t3, backupDir, "--primary", accepting.origin, "--no-fallback"), `${key}\n`), /may hold only letters, digits/);
  }
  await refused(await run("--primary", "http://127.0.0.1:9/v1", "--no-fallback"), /primary must be an HTTP origin/);
  await refused(await run("--primary", accepting.origin, "--fallback", `${accepting.origin}/`), /different CLIProxy host/);
  assert.equal(accepting.requests.length, checked);

  const skipped = await run("--primary", accepting.origin, "--fallback", down, "--no-key-check");
  assert.equal(skipped.code, 0, skipped.stderr);
  assert.equal(accepting.requests.length, checked);
  assert.deepEqual(t3.calls(), ["issue", "revoke"]);
  assert.equal(value(t3.pool().environment, "CLIPROXY_FALLBACK_URL"), down);
});
