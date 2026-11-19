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
const descriptorCodex = `#!/usr/bin/env python3
import fcntl
import json
import os
import sys

nonblocking = [bool(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK) for fd in range(3)]
print(json.dumps({"args": sys.argv[1:], "pid": os.getpid(), "nonblocking": nonblocking}))
sys.stderr.write(sys.stdin.read())
sys.exit(37)
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

function start(t, args, { fallback, action, fake = fakeCodex, sibling } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "codex-launcher-"));
  const binary = path.join(dir, "fake codex.cjs");
  const log = path.join(dir, "runtime", "codex-failover.jsonl");
  fs.writeFileSync(binary, fake, { mode: 0o700 });
  let executable = launcher;
  if (sibling !== undefined) {
    executable = path.join(dir, "codex_fallback_launcher.mjs");
    fs.copyFileSync(launcher, executable);
    if (sibling !== null) fs.writeFileSync(path.join(dir, "claude_account_launcher.mjs"), sibling);
  }
  const env = {
    PATH: process.env.PATH,
    CODEX_REAL_BINARY: binary,
    CLIPROXY_CODEX_FAILOVER_LOG: log,
  };
  if (fallback) env.CLIPROXY_FALLBACK_URL = fallback;
  if (action) env.FAKE_CODEX_ACTION = action;
  const child = spawn(executable, args, { env, stdio: ["pipe", "pipe", "pipe"] });
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

async function launch(t, args, fallback, options) {
  const run = start(t, args, { fallback, ...options });
  run.child.stdin.end("fake stdin\n");
  assert.deepEqual(await run.done, [37, null]);
  assert.equal(run.stderr(), "fake stdin\n");
  return { ...run, args: (await run.ready).args };
}

function assertDecision(log, selected, reason, subcommand = "app-server") {
  const lines = fs.readFileSync(log, "utf8").trimEnd().split("\n");
  assert.equal(lines.length, 1);
  const { ts, pid, ...decision } = JSON.parse(lines[0]);
  assert.ok(Number.isFinite(Date.parse(ts)));
  assert.ok(Number.isInteger(pid));
  // No URL or launch arguments enter the record.
  assert.deepEqual(decision, { selected, reason, subcommand });
  assert.equal(fs.statSync(log).mode & 0o777, 0o600);
}

const healthRequest = { method: "GET", url: "/healthz", authorization: undefined, connection: "close" };

test("healthy primary wins even when the concurrent fallback answers first", { timeout: 5000 }, async (t) => {
  const { promise: fallbackAnswered, resolve } = Promise.withResolvers();
  const primary = await fakeHealth(t, async (res) => {
    await fallbackAnswered;
    res.end(healthyBody);
  });
  const fallback = await fakeHealth(t, (res) => res.end(healthyBody, resolve));
  const args = t3Args(primary.url);
  const run = await launch(t, args, fallback.url);
  assert.deepEqual(run.args, args);
  assert.deepEqual(primary.requests, [healthRequest]);
  assert.deepEqual(fallback.requests, [healthRequest]);
  assertDecision(run.log, "primary", "primary_healthy");
});

test("a healthy primary cancels a stalled fallback probe", { timeout: 5000 }, async (t) => {
  const { promise: fallbackRequested, resolve: requested } = Promise.withResolvers();
  const { promise: fallbackClosed, resolve: closed } = Promise.withResolvers();
  const primary = await fakeHealth(t, async (res) => {
    await fallbackRequested;
    res.end(healthyBody);
  });
  const fallback = await fakeHealth(t, (res) => {
    res.once("close", closed);
    requested();
  });
  const args = t3Args(primary.url);
  const started = performance.now();
  const run = await launch(t, args, fallback.url);
  await fallbackClosed;
  assert.ok(performance.now() - started < 2800);
  assert.deepEqual(run.args, args);
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

test("two silent hosts share one probe deadline", { timeout: 8000 }, async (t) => {
  const primary = await fakeHealth(t, () => {});
  const fallback = await fakeHealth(t, () => {});
  const args = t3Args(primary.url);
  const started = performance.now();
  const run = await launch(t, args, fallback.url);
  const elapsed = performance.now() - started;
  assert.ok(elapsed >= 2900 && elapsed < 5000, `expected one probe deadline, got ${elapsed} ms`);
  assert.deepEqual(run.args, args);
  assert.deepEqual(primary.requests, [healthRequest]);
  assert.deepEqual(fallback.requests, [healthRequest]);
  assertDecision(run.log, "primary", "both_unhealthy");
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

test("passthrough keeps stdin, stdout and stderr blocking across execve", async (t) => {
  const args = ["--version"];
  const run = await launch(t, args, undefined, { fake: descriptorCodex });
  const child = await run.ready;
  assert.deepEqual(child.nonblocking, [false, false, false]);
  assert.deepEqual(child.args, args);
  if (process.execve) assert.equal(child.pid, run.child.pid);
});

test("passthrough works with no sibling launcher", async (t) => {
  const args = ["--version"];
  const run = await launch(t, args, undefined, { sibling: null });
  assert.deepEqual(run.args, args);
  assert.equal(fs.existsSync(run.log), false);
});

for (const [name, sibling] of [
  ["missing", null],
  ["broken", "export const = ;"],
  ["throwing", "export function probeHealth() { throw new TypeError('probe drift'); }"],
  ["rejecting", "export async function probeHealth() { throw new Error('probe drift'); }"],
]) {
  test(`an opted-in launch with a ${name} sibling keeps the original args`, async (t) => {
    const primary = await fakeHealth(t);
    const fallback = await fakeHealth(t);
    const args = t3Args(primary.url);
    const run = await launch(t, args, fallback.url, { sibling });
    assert.deepEqual(run.args, args);
    assert.deepEqual(primary.requests, []);
    assert.deepEqual(fallback.requests, []);
    assertDecision(run.log, "primary", "invalid_configuration");
  });
}

test("a shipped Mac bundle logs decisions under the instance root", async (t) => {
  // A real home path, like /Users/rever; macOS's tmpdir sits behind the /var symlink.
  const home = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "codex-bundle-home-")));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  // Same layout fleet_bundle.py publishes: alias -> .fleet-current -> .fleet-releases/<stamp>/ops.
  const root = path.join(home, "cliproxy-live", "ops");
  const release = path.join(root, ".fleet-releases", "20261118T000000Z-test", "ops");
  fs.mkdirSync(release, { recursive: true });
  for (const name of ["codex_fallback_launcher.mjs", "claude_account_launcher.mjs"]) {
    fs.copyFileSync(path.join(path.dirname(launcher), name), path.join(release, name));
    fs.chmodSync(path.join(release, name), 0o755);
    fs.symlinkSync(path.join(".fleet-current", name), path.join(root, name));
  }
  fs.symlinkSync(path.relative(root, release), path.join(root, ".fleet-current"));
  const binary = path.join(home, "fake-codex.cjs");
  fs.writeFileSync(binary, fakeCodex, { mode: 0o700 });
  const primary = await fakeHealth(t);
  const fallback = await fakeHealth(t);
  const args = t3Args(primary.url);
  const child = spawn(path.join(root, "codex_fallback_launcher.mjs"), args, {
    env: { PATH: process.env.PATH, HOME: home, CODEX_REAL_BINARY: binary, CLIPROXY_FALLBACK_URL: fallback.url },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  child.stdout.setEncoding("utf8").on("data", (chunk) => { stdout += chunk; });
  assert.deepEqual(await once(child, "close"), [37, null]);
  assert.deepEqual(JSON.parse(stdout).args, args);
  assertDecision(path.join(home, "cliproxy", "ops", "runtime", "fleet", "codex-failover.jsonl"),
    "primary", "primary_healthy");
  assert.equal(fs.existsSync(path.join(release, "runtime")), false);
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

const overrideForms = [
  ["short attached", (value) => [`-c${value}`]],
  ["short equals", (value) => [`-c=${value}`]],
  ["long equals", (value) => [`--config=${value}`]],
  ["long separate", (value) => ["--config", value]],
];

for (const [name, config] of overrideForms) {
  test(`rewrites only the URL in a ${name} override`, async (t) => {
    const primary = await fakeHealth(t, (res) => res.writeHead(503).end());
    const fallback = await fakeHealth(t);
    const primaryOverride = config(`${baseURLKey} = '${primary.url}/v1/' `);
    const fallbackOverride = config(`${baseURLKey} = '${fallback.url}/v1/' `);
    const args = t3Args(primary.url).toSpliced(2, 2, ...primaryOverride);
    const run = await launch(t, args, fallback.url);
    assert.deepEqual(run.args, args.toSpliced(2, primaryOverride.length, ...fallbackOverride));
    assert.deepEqual(primary.requests, [healthRequest]);
    assert.deepEqual(fallback.requests, [healthRequest]);
    assertDecision(run.log, "fallback", "fallback_healthy");
  });

  test(`rejects a duplicate ${name} override after the T3 override`, async (t) => {
    const primary = await fakeHealth(t);
    const fallback = await fakeHealth(t);
    const args = [...t3Args(primary.url), ...config(`${baseURLKey}="${fallback.url}/v1"`)];
    const run = await launch(t, args, fallback.url);
    assert.deepEqual(run.args, args);
    assert.deepEqual(primary.requests, []);
    assert.deepEqual(fallback.requests, []);
    assertDecision(run.log, "primary", "invalid_configuration");
  });
}

for (const [name, override] of [
  ["provider table", (url) => `--config=model_providers.cliproxy={base_url="${url}/v1"}`],
  ["quoted key", (url) => `-cmodel_providers."cliproxy".base_url="${url}/v1"`],
  ["quoted key with spaces", (url) => `--config="model_providers" . 'cliproxy' . "base_url" = "${url}/v1"`],
  ["escaped key", (url) => String.raw`-c=model_providers."cli\u0070roxy".'base_url'="${url}/v1"`],
]) {
  for (const placement of ["alone", "after"]) {
    test(`keeps a ${name} override ${placement} without probing`, async (t) => {
      const primary = await fakeHealth(t);
      const fallback = await fakeHealth(t);
      const config = override(primary.url);
      const args = placement === "alone" ? ["app-server", config] : [...t3Args(primary.url), config];
      const run = await launch(t, args, fallback.url);
      assert.deepEqual(run.args, args);
      assert.deepEqual(primary.requests, []);
      assert.deepEqual(fallback.requests, []);
      assertDecision(run.log, "primary", "invalid_configuration");
    });
  }
}

for (const [subcommand, tail] of [
  ["exec", ["-m", "mcp", "exec", "synthetic prompt"]],
  ["mcp", ["mcp", "list", "--json"]],
  ["none", []],
  ["none", ["synthetic prompt", "exec"]],
]) {
  test(`logs ${subcommand} for argv ending in ${JSON.stringify(tail)}`, async (t) => {
    const primary = await fakeHealth(t);
    const fallback = await fakeHealth(t);
    const args = [...t3Args(primary.url).slice(0, -3), ...tail];
    const run = await launch(t, args, fallback.url);
    assert.deepEqual(run.args, args);
    assert.deepEqual(primary.requests, [healthRequest]);
    assertDecision(run.log, "primary", "primary_healthy", subcommand);
  });
}

for (const args of [["--version"], ["mcp", "list", "--json"], ["exec", "--", `-c${baseURLKey}="ignored"`]]) {
  test(`skips probing and logging for argv without config overrides ${JSON.stringify(args)}`, async (t) => {
    const fallback = await fakeHealth(t);
    const run = await launch(t, args, fallback.url, { sibling: null });
    assert.deepEqual(run.args, args);
    assert.deepEqual(fallback.requests, []);
    assert.equal(fs.existsSync(run.log), false);
    if (process.execve) assert.equal((await run.ready).pid, run.child.pid);
  });
}

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
