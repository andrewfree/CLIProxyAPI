import test from "node:test";
import assert from "node:assert/strict";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";
import http from "node:http";
import { once } from "node:events";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const launcher = fileURLToPath(new URL("./claude_account_launcher.mjs", import.meta.url));

const stubSource = `
const mode = process.argv[2];
const arg = process.argv[3];
const keepAlive = setInterval(() => {}, 1000);
if (mode === "ignore") {
  for (const s of ["SIGTERM", "SIGINT", "SIGHUP"]) process.on(s, () => console.log("got " + s));
} else if (mode === "graceful") {
  process.on("SIGTERM", () => process.exit(0));
}
console.log("READY pid=" + process.pid + " url=" + process.env.ANTHROPIC_BASE_URL);
if (mode === "exit") process.exit(Number(arg));
if (mode === "die") process.kill(process.pid, "SIGTERM");
if (mode === "count") {
  const res = await fetch(process.env.ANTHROPIC_BASE_URL + "/v1/messages/count_tokens", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ model: "claude-opus-5-5", messages: [] }),
  });
  console.log("RESULT host=" + res.headers.get("x-cliproxy-host") + " status=" + res.status + " body=" + (await res.text()));
  process.exit(0);
}
`;

function isAlive(pid) {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

function canConnect(url) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: "127.0.0.1", port: Number(new URL(url).port) });
    socket.once("connect", () => { socket.destroy(); resolve(true); });
    socket.once("error", () => resolve(false));
  });
}

function withDeadline(promise, ms, label) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`Timed out waiting for ${label}`)), ms);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

async function waitFor(predicate, label, ms = 15000) {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > ms) throw new Error(`Timed out waiting for ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

async function run(t, { binary, args = [], env = {} }) {
  const upstream = net.createServer((socket) => socket.destroy());
  upstream.listen(0, "127.0.0.1");
  await new Promise((resolve, reject) => { upstream.once("listening", resolve); upstream.once("error", reject); });
  t.after(() => new Promise((resolve) => upstream.close(resolve)));
  const childEnv = {
    ...process.env,
    CLIPROXY_CLAUDE_BINARY: binary,
    CLIPROXY_CLAUDE_ROUTE: "account",
    CLIPROXY_ACCOUNT_PREFIX: "acct",
    CLIPROXY_BASE_URL: `http://127.0.0.1:${upstream.address().port}`,
    ...env,
  };
  for (const [name, value] of Object.entries(childEnv)) if (value === null) delete childEnv[name];
  const proc = spawn(launcher, args, { stdio: ["ignore", "pipe", "pipe"], env: childEnv });
  let stdout = "";
  let stderr = "";
  proc.stdout.on("data", (chunk) => { stdout += chunk; });
  proc.stderr.on("data", (chunk) => { stderr += chunk; });
  const exited = new Promise((resolve) => proc.once("exit", (code, signal) => resolve({ code, signal })));
  const run = {
    proc, exited,
    stdout: () => stdout,
    stderr: () => stderr,
    ready() {
      const match = /READY pid=(\d+) url=(\S+)/.exec(stdout);
      return match ? { pid: Number(match[1]), url: match[2] } : null;
    },
  };
  t.after(async () => {
    const info = run.ready();
    if (info && isAlive(info.pid)) { try { process.kill(info.pid, "SIGKILL"); } catch {} }
    if (proc.exitCode === null && proc.signalCode === null) proc.kill("SIGKILL");
    await withDeadline(exited, 5000, "test process cleanup");
  });
  return run;
}

function launch(t, ...stubArgs) {
  return launchWith(t, {}, ...stubArgs);
}

function launchWith(t, env, ...stubArgs) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "launcher-stub-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const stub = path.join(dir, "stub.mjs");
  fs.writeFileSync(stub, stubSource);
  return run(t, { binary: process.execPath, args: [stub, ...stubArgs], env });
}

async function started(r) {
  await waitFor(() => r.ready(), "child readiness");
  const info = r.ready();
  assert.match(info.url, /^http:\/\/127\.0\.0\.1:\d+$/);
  return info;
}

for (const code of [0, 7]) {
  test(`propagates ordinary child exit code ${code}`, async (t) => {
    const r = await launch(t, "exit", String(code));
    const result = await withDeadline(r.exited, 15000, "launcher exit");
    assert.deepEqual(result, { code, signal: null });
    const info = r.ready();
    assert.ok(info);
    assert.equal(await canConnect(info.url), false);
  });
}

test("reports 128 + signal number when the child is killed by a signal", async (t) => {
  const r = await launch(t, "die");
  const result = await withDeadline(r.exited, 15000, "launcher exit");
  assert.deepEqual(result, { code: 128 + os.constants.signals.SIGTERM, signal: null });
});

test("graceful child: SIGTERM closes the listener and exits 0", async (t) => {
  const r = await launch(t, "graceful");
  const info = await started(r);
  assert.equal(await canConnect(info.url), true);
  r.proc.kill("SIGTERM");
  const result = await withDeadline(r.exited, 15000, "launcher exit");
  assert.deepEqual(result, { code: 0, signal: null });
  assert.equal(await canConnect(info.url), false);
  assert.equal(isAlive(info.pid), false);
});

test("SIGTERM-ignoring child is force-killed and the launcher exits", async (t) => {
  const r = await launch(t, "ignore");
  const info = await started(r);
  r.proc.kill("SIGTERM");
  await waitFor(() => r.stdout().includes("got SIGTERM"), "child to receive SIGTERM");
  assert.equal(await canConnect(info.url), false);
  const result = await withDeadline(r.exited, 30000, "forced shutdown");
  assert.deepEqual(result, { code: 128 + os.constants.signals.SIGKILL, signal: null });
  assert.equal(isAlive(info.pid), false);
});

test("repeated signals are handled once and still terminate", async (t) => {
  const r = await launch(t, "ignore");
  const info = await started(r);
  r.proc.kill("SIGTERM");
  await waitFor(() => r.stdout().includes("got SIGTERM"), "first forwarded signal");
  r.proc.kill("SIGINT");
  r.proc.kill("SIGTERM");
  const result = await withDeadline(r.exited, 30000, "forced shutdown");
  assert.equal(result.code, 128 + os.constants.signals.SIGKILL);
  assert.equal(r.stdout().match(/got SIGTERM/g)?.length, 1);
  assert.doesNotMatch(r.stdout(), /got SIGINT/);
  assert.equal(isAlive(info.pid), false);
});

test("spawn failure cleans up and exits nonzero", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "launcher-missing-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const r = await run(t, { binary: path.join(dir, "does-not-exist") });
  const result = await withDeadline(r.exited, 15000, "launcher exit");
  assert.deepEqual(result, { code: 1, signal: null });
  assert.match(r.stderr(), /Unable to launch Claude Code\./);
});

test("shared-pool route launches Claude without an account prefix", async (t) => {
  const r = await launchWith(t, { CLIPROXY_CLAUDE_ROUTE: "shared-pool", CLIPROXY_ACCOUNT_PREFIX: null }, "exit", "0");
  const result = await withDeadline(r.exited, 15000, "launcher exit");
  assert.deepEqual(result, { code: 0, signal: null });
  assert.ok(r.ready());
});

test("a shared-pool route that also names an account fails before launching Claude", async (t) => {
  const r = await launchWith(t, { CLIPROXY_CLAUDE_ROUTE: "shared-pool" }, "exit", "0");
  const result = await withDeadline(r.exited, 15000, "launcher exit");
  assert.deepEqual(result, { code: 1, signal: null });
  assert.equal(r.ready(), null);
  assert.match(r.stderr(), /shared-pool route must not set CLIPROXY_ACCOUNT_PREFIX/);
});

test("a fallback on a named account route fails before launching Claude", async (t) => {
  const r = await launchWith(t, { CLIPROXY_FALLBACK_URL: "http://127.0.0.1:9" }, "exit", "0");
  assert.deepEqual(await withDeadline(r.exited, 15000, "launcher exit"), { code: 1, signal: null });
  assert.equal(r.ready(), null);
  assert.match(r.stderr(), /only supported on the shared-pool route/);
});

test("an invalid connect timeout fails before launching Claude", async (t) => {
  const r = await launchWith(t, {
    CLIPROXY_CLAUDE_ROUTE: "shared-pool", CLIPROXY_ACCOUNT_PREFIX: null,
    CLIPROXY_FALLBACK_URL: "http://127.0.0.1:9", CLIPROXY_CONNECT_TIMEOUT_MS: "soon",
  }, "exit", "0");
  assert.deepEqual(await withDeadline(r.exited, 15000, "launcher exit"), { code: 1, signal: null });
  assert.match(r.stderr(), /CLIPROXY_CONNECT_TIMEOUT_MS/);
});

test("without a fallback, the connect timeout variable is ignored", async (t) => {
  const r = await launchWith(t, { CLIPROXY_CONNECT_TIMEOUT_MS: "soon" }, "exit", "0");
  assert.deepEqual(await withDeadline(r.exited, 15000, "launcher exit"), { code: 0, signal: null });
  assert.ok(r.ready());
});

test("with a fallback, an unhealthy primary sends Claude's first request to the fallback", async (t) => {
  const fallback = http.createServer((_req, res) => res.end('{"input_tokens":1}'));
  fallback.listen(0, "127.0.0.1");
  await once(fallback, "listening");
  t.after(() => { fallback.closeAllConnections(); fallback.close(); });
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "launcher-failover-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const logFile = path.join(dir, "failover.jsonl");
  // The default primary resets every connection, so its health probe fails.
  const r = await launchWith(t, {
    CLIPROXY_CLAUDE_ROUTE: "shared-pool", CLIPROXY_ACCOUNT_PREFIX: null,
    CLIPROXY_FALLBACK_URL: `http://127.0.0.1:${fallback.address().port}`, CLIPROXY_FAILOVER_LOG: logFile,
  }, "count");
  assert.deepEqual(await withDeadline(r.exited, 15000, "launcher exit"), { code: 0, signal: null });
  assert.match(r.stdout(), /RESULT host=fallback status=200 body=\{"input_tokens":1\}/);
  await waitFor(() => fs.existsSync(logFile) && fs.readFileSync(logFile, "utf8").includes("\n"), "transition log");
  const [entry] = fs.readFileSync(logFile, "utf8").trim().split("\n").map((line) => JSON.parse(line));
  assert.equal(entry.event, "primary_down");
  assert.equal(entry.reason, "startup_probe");
  assert.equal((fs.statSync(logFile).mode & 0o777).toString(8), "600");
});
