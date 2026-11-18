import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import test from "node:test";
import { fileURLToPath } from "node:url";

const launcher = fileURLToPath(new URL("./codex_fallback_launcher.mjs", import.meta.url));
const baseURLKey = "model_providers.cliproxy.base_url";
const healthyBody = '{"status":"ok"}';
const fakeCodex = String.raw`#!/usr/bin/env node
const action = process.env.FAKE_CODEX_ACTION;
if (action === "wait") {
  for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) {
    process.on(signal, () => {
      process.stderr.write(signal, () => process.exit(23));
    });
  }
  process.stdin.resume();
}
console.log(JSON.stringify({ args: process.argv.slice(2), pid: process.pid }));
if (action === "wait") return;
if (action === "die") {
  process.kill(process.pid, "SIGTERM");
} else {
  process.stdin.pipe(process.stderr);
  process.on("beforeExit", () => { process.exitCode = 37; });
}
`;

function t3Args(primary) {
  return [
    "-c", 'model_provider="cliproxy"',
    "-c", `${baseURLKey}="${primary}/v1"`,
    "-c", 'model_providers.cliproxy.auth.command="/some path/cliproxy-codex-token"',
    "-c", 'model_providers.another.base_url="http://127.0.0.1:1/v1"',
    "-c", 'model="synthetic model"',
    "app-server", "--listen", "stdio://",
  ];
}

async function fakeHealth(t, respond = (res) => res.end(healthyBody)) {
  const requests = [];
  const server = http.createServer((req, res) => {
    requests.push({ method: req.method, url: req.url, authorization: req.headers.authorization,
      connection: req.headers.connection });
    respond(res);
  });
  const closed = () => new Promise((resolve) => {
    server.close(resolve);
    server.closeAllConnections();
  });
  t.after(closed);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return { url: `http://127.0.0.1:${server.address().port}`, requests, closed };
}

function start(t, args, { fallback, action } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "codex-launcher-"));
  const binary = path.join(dir, "fake codex.cjs");
  const log = path.join(dir, "runtime", "codex-failover.jsonl");
  fs.writeFileSync(binary, fakeCodex, { mode: 0o700 });
  const env = {
    PATH: process.env.PATH,
    CODEX_REAL_BINARY: binary,
    CLIPROXY_CODEX_FAILOVER_LOG: log,
  };
  if (fallback) env.CLIPROXY_FALLBACK_URL = fallback;
  if (action) env.FAKE_CODEX_ACTION = action;
  const child = spawn(launcher, args, { env, stdio: ["pipe", "pipe", "pipe"] });
  let stderr = "";
  child.stderr.setEncoding("utf8").on("data", (chunk) => { stderr += chunk; });
  const lines = readline.createInterface({ input: child.stdout });
  const ready = once(lines, "line").then(([line]) => JSON.parse(line));
  const done = once(child, "close");
  t.after(async () => {
    child.stdin.end();
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
    await done;
    fs.rmSync(dir, { recursive: true, force: true });
  });
  return { child, ready, done, log, stderr: () => stderr };
}

async function launch(t, args, fallback) {
  const run = start(t, args, { fallback });
  run.child.stdin.end("fake stdin\n");
  assert.deepEqual(await run.done, [37, null]);
  assert.equal(run.stderr(), "fake stdin\n");
  return { ...run, args: (await run.ready).args };
}

function assertDecision(log, selected, reason) {
  const lines = fs.readFileSync(log, "utf8").trimEnd().split("\n");
  assert.equal(lines.length, 1);
  const { ts, pid, ...decision } = JSON.parse(lines[0]);
  assert.ok(Number.isFinite(Date.parse(ts)));
  assert.ok(Number.isInteger(pid));
  // No URL or launch arguments enter the record.
  assert.deepEqual(decision, { selected, reason });
  assert.equal(fs.statSync(log).mode & 0o777, 0o600);
}

const healthRequest = { method: "GET", url: "/healthz", authorization: undefined, connection: "close" };

test("healthy primary runs the T3 argv unchanged and skips the fallback", async (t) => {
  const primary = await fakeHealth(t);
  const fallback = await fakeHealth(t);
  const args = t3Args(primary.url);
  const run = await launch(t, args, fallback.url);
  assert.deepEqual(run.args, args);
  assert.deepEqual(primary.requests, [healthRequest]);
  assert.deepEqual(fallback.requests, []);
  assertDecision(run.log, "primary", "primary_healthy");
});

test("connection refusal rewrites only the T3 base_url value", async (t) => {
  const primary = await fakeHealth(t);
  const fallback = await fakeHealth(t);
  await primary.closed();
  const args = t3Args(primary.url);
  args[3] = ` ${baseURLKey} = "${primary.url}/v1"  `;
  const run = await launch(t, args, fallback.url);
  assert.deepEqual(run.args, args.with(3, ` ${baseURLKey} = "${fallback.url}/v1"  `));
  assert.deepEqual(fallback.requests, [healthRequest]);
  assertDecision(run.log, "fallback", "fallback_healthy");
});

for (const [status, body] of [[503, healthyBody], [200, '{"status":"down"}'], [200, "<html>hello</html>"]]) {
  test(`fallback handles health response ${status} ${body}`, async (t) => {
    const primary = await fakeHealth(t, (res) => res.writeHead(status).end(body));
    const fallback = await fakeHealth(t);
    const args = t3Args(primary.url);
    const run = await launch(t, args, fallback.url);
    assert.deepEqual(run.args, t3Args(fallback.url));
    assert.deepEqual(primary.requests, [healthRequest]);
    assert.deepEqual(fallback.requests, [healthRequest]);
    assertDecision(run.log, "fallback", "fallback_healthy");
  });
}

test("the total probe deadline includes a stalled health response body", { timeout: 8000 }, async (t) => {
  const primary = await fakeHealth(t, (res) => res.write('{"status":'));
  const fallback = await fakeHealth(t);
  const started = performance.now();
  const run = await launch(t, t3Args(primary.url), fallback.url);
  assert.ok(performance.now() - started >= 2900);
  assert.deepEqual(run.args, t3Args(fallback.url));
  assert.deepEqual(primary.requests, [healthRequest]);
  assert.deepEqual(fallback.requests, [healthRequest]);
  assertDecision(run.log, "fallback", "fallback_healthy");
});

test("both hosts down keeps the primary args", async (t) => {
  const primary = await fakeHealth(t);
  const fallback = await fakeHealth(t);
  await Promise.all([primary.closed(), fallback.closed()]);
  const args = t3Args(primary.url);
  const run = await launch(t, args, fallback.url);
  assert.deepEqual(run.args, args);
  assertDecision(run.log, "primary", "both_unhealthy");
});

test("an unhealthy fallback keeps the primary args", async (t) => {
  const primary = await fakeHealth(t, (res) => res.writeHead(503).end(healthyBody));
  const fallback = await fakeHealth(t, (res) => res.end('{"status":"down"}'));
  const args = t3Args(primary.url);
  const run = await launch(t, args, fallback.url);
  assert.deepEqual(run.args, args);
  assert.deepEqual(primary.requests, [healthRequest]);
  assert.deepEqual(fallback.requests, [healthRequest]);
  assertDecision(run.log, "primary", "both_unhealthy");
});

test("without the opt-in there is no probe or log and argv is unchanged", async (t) => {
  const primary = await fakeHealth(t);
  const args = t3Args(primary.url);
  const run = await launch(t, args);
  assert.deepEqual(run.args, args);
  assert.deepEqual(primary.requests, []);
  assert.equal(fs.existsSync(run.log), false);
  if (process.execve) assert.equal((await run.ready).pid, run.child.pid);
});

test("ambiguous base_url overrides bypass probing and keep argv", async (t) => {
  const primary = await fakeHealth(t);
  const fallback = await fakeHealth(t);
  const args = ["-c", `${baseURLKey}="${fallback.url}/v1"`, ...t3Args(primary.url)];
  const run = await launch(t, args, fallback.url);
  assert.deepEqual(run.args, args);
  assert.deepEqual(primary.requests, []);
  assert.deepEqual(fallback.requests, []);
  assertDecision(run.log, "primary", "invalid_configuration");
});

for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) {
  test(`forwards ${signal} and preserves the child's exit code`, { timeout: 8000 }, async (t) => {
    const primary = await fakeHealth(t);
    const fallback = await fakeHealth(t);
    const run = start(t, t3Args(primary.url), { fallback: fallback.url, action: "wait" });
    await run.ready;
    run.child.kill(signal);
    assert.deepEqual(await run.done, [23, null]);
    assert.equal(run.stderr(), signal);
  });
}

test("reports a child signal exit as 128 plus the signal number", async (t) => {
  const primary = await fakeHealth(t);
  const fallback = await fakeHealth(t);
  const run = start(t, t3Args(primary.url), { fallback: fallback.url, action: "die" });
  assert.deepEqual(await run.done, [128 + os.constants.signals.SIGTERM, null]);
});
