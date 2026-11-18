import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { maskHosts, runDrill, splitPrimaries } from "./claude_failover_drill.mjs";

const drill = fileURLToPath(new URL("./claude_failover_drill.mjs", import.meta.url));
const launcher = fileURLToPath(new URL("./claude_account_launcher.mjs", import.meta.url));
const fakeKey = "fake-drill-key-0123";

function tempDir(t, prefix) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

// HOME points at an empty directory, so the real key command cannot run even if the drill looked for it.
function drillEnv(t) {
  return { PATH: process.env.PATH, HOME: tempDir(t, "drill-home-"), CLIPROXY_DRILL_KEY: fakeKey };
}

// A CLIProxy stand-in: healthy, and answers count_tokens with one token.
async function fakeProxy(t) {
  const requests = [];
  const server = http.createServer(async (request, response) => {
    if (request.method === "GET" && request.url === "/healthz") {
      response.writeHead(200, { "content-type": "application/json" });
      return response.end('{"status":"ok"}');
    }
    if (request.method === "POST" && request.url === "/v1/messages/count_tokens") {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      requests.push({ key: request.headers["x-api-key"], model: JSON.parse(Buffer.concat(chunks)).model });
      response.writeHead(200, { "content-type": "application/json" });
      return response.end('{"input_tokens":1}');
    }
    response.writeHead(404);
    response.end();
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => { server.closeAllConnections(); server.close(); });
  return { origin: `http://127.0.0.1:${server.address().port}`, port: server.address().port, requests };
}

async function closedOrigin() {
  const server = http.createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const { port } = server.address();
  await new Promise((resolve) => server.close(resolve));
  return `http://127.0.0.1:${port}`;
}

function quiet() {
  const stderr = [];
  return { onStderr: (text) => stderr.push(text), stderr };
}

function withoutTimings(result) {
  assert.equal(typeof result.totalSeconds, "number");
  assert.equal(typeof result.ms, "number");
  const { totalSeconds, ms, ...rest } = result;
  return rest;
}

test("a closed primary port sends the request to the fallback after a startup probe failure", async (t) => {
  const fallback = await fakeProxy(t);
  const primary = await closedOrigin();
  const { ok, results } = await runDrill({ primaries: [primary], fallback: fallback.origin, env: drillEnv(t), ...quiet() });
  assert.equal(ok, true);
  assert.equal(results.length, 1);
  assert.deepEqual(withoutTimings(results[0]), {
    primary, exit: 0, status: 200, host: "fallback", inputTokens: 1, transitions: [["primary_down", "startup_probe"]],
  });
  assert.deepEqual(fallback.requests, [{ key: fakeKey, model: "claude-haiku-5-5" }]);
  assert.doesNotMatch(JSON.stringify(results), new RegExp(fakeKey));
});

test("a healthy primary serves the request with no transitions", async (t) => {
  const fallback = await fakeProxy(t);
  const primary = await fakeProxy(t);
  const { ok, results } = await runDrill({
    primaries: [primary.origin], fallback: fallback.origin, model: "claude-opus-5-5", env: drillEnv(t), ...quiet(),
  });
  assert.equal(ok, true);
  assert.deepEqual(withoutTimings(results[0]), {
    primary: primary.origin, exit: 0, status: 200, host: "primary", inputTokens: 1, transitions: [],
  });
  assert.deepEqual(primary.requests, [{ key: fakeKey, model: "claude-opus-5-5" }]);
  assert.deepEqual(fallback.requests, []);
});

test("a ts.net primary is masked in the output", async (t) => {
  const fallback = await fakeProxy(t);
  const primary = await fakeProxy(t);
  // Runs the real launcher with *.ts.net names resolved to 127.0.0.1, so no DNS query leaves the host.
  const wrapper = path.join(tempDir(t, "drill-launcher-"), "launcher.mjs");
  fs.writeFileSync(wrapper, `import dns from "node:dns";
const lookup = dns.lookup;
dns.lookup = (host, ...rest) => lookup(String(host).endsWith(".ts.net") ? "127.0.0.1" : host, ...rest);
process.argv[1] = ${JSON.stringify(launcher)};
await import(${JSON.stringify(pathToFileURL(launcher).href)});
`);
  const { ok, results } = await runDrill({
    primaries: [`http://will.tailabc123.ts.net:${primary.port}`], fallback: fallback.origin,
    launcher: wrapper, env: drillEnv(t), ...quiet(),
  });
  assert.equal(ok, true);
  assert.equal(results[0].primary, `http://will.<tailnet>.ts.net:${primary.port}`);
  assert.equal(results[0].host, "primary");
  assert.equal(primary.requests.length, 1);
  assert.doesNotMatch(JSON.stringify(results), /tailabc123/);
});

test("maskHosts hides only the tailnet name", () => {
  assert.equal(maskHosts("http://will.tail1a2b.ts.net:8317"), "http://will.<tailnet>.ts.net:8317");
  assert.equal(maskHosts("lookup tail1a2b.ts.net failed"), "lookup <tailnet>.ts.net failed");
  assert.equal(maskHosts("http://will.<tailnet>.ts.net:8317"), "http://will.<tailnet>.ts.net:8317");
  assert.equal(maskHosts("http://127.0.0.1:8317"), "http://127.0.0.1:8317");
});

test("primaries may be repeated or comma separated", () => {
  assert.deepEqual(splitPrimaries(["http://a:1, http://b:2", "http://c:3"]), ["http://a:1", "http://b:2", "http://c:3"]);
  assert.deepEqual(splitPrimaries(undefined), []);
});

test("an explicit key command supplies the key", async (t) => {
  const fallback = await fakeProxy(t);
  const command = path.join(tempDir(t, "drill-key-"), "key");
  fs.writeFileSync(command, "#!/bin/sh\nprintf 'from-command-key\\n'\n", { mode: 0o700 });
  const { ok } = await runDrill({
    primaries: [await closedOrigin()], fallback: fallback.origin, keyCommand: command, env: drillEnv(t), ...quiet(),
  });
  assert.equal(ok, true);
  assert.deepEqual(fallback.requests.map((request) => request.key), ["from-command-key"]);
});

test("a failing key command stops the drill before any run", async (t) => {
  const fallback = await fakeProxy(t);
  const command = path.join(tempDir(t, "drill-key-"), "key");
  fs.writeFileSync(command, "#!/bin/sh\nexit 3\n", { mode: 0o700 });
  await assert.rejects(
    runDrill({ primaries: [fallback.origin], fallback: fallback.origin, keyCommand: command, env: drillEnv(t), ...quiet() }),
    /The key command failed/,
  );
  assert.deepEqual(fallback.requests, []);
});

test("a run that outlives its deadline is stopped and reported as failed", async (t) => {
  const hanging = http.createServer((request, response) => {
    if (request.url === "/healthz") return response.end('{"status":"ok"}');
    // count_tokens is never answered.
  });
  hanging.listen(0, "127.0.0.1");
  await once(hanging, "listening");
  t.after(() => { hanging.closeAllConnections(); hanging.close(); });
  const { ok, results } = await runDrill({
    primaries: [`http://127.0.0.1:${hanging.address().port}`], fallback: await closedOrigin(),
    deadlineMs: 500, env: drillEnv(t), ...quiet(),
  });
  assert.equal(ok, false);
  assert.equal(results[0].status, null);
  assert.notEqual(results[0].exit, 0);
  assert.ok(results[0].totalSeconds < 10);
});

async function runCli(t, args) {
  const proc = spawn(process.execPath, [drill, ...args], { stdio: ["ignore", "pipe", "pipe"], env: drillEnv(t) });
  let stdout = "";
  let stderr = "";
  proc.stdout.on("data", (chunk) => { stdout += chunk; });
  proc.stderr.on("data", (chunk) => { stderr += chunk; });
  const [code] = await once(proc, "close");
  return { code, stdout, stderr, lines: stdout.trim().split("\n").filter(Boolean).map((line) => JSON.parse(line)) };
}

test("the CLI prints one JSON line per primary and exits 0 when every run returned 200", async (t) => {
  const fallback = await fakeProxy(t);
  const healthy = await fakeProxy(t);
  const closed = await closedOrigin();
  const { code, stdout, stderr, lines } = await runCli(t, [
    "--primary", `${closed},${healthy.origin}`, "--primary", closed, "--fallback", fallback.origin,
  ]);
  assert.equal(code, 0, stderr);
  assert.deepEqual(lines.map((line) => [line.primary, line.status, line.host]), [
    [closed, 200, "fallback"], [healthy.origin, 200, "primary"], [closed, 200, "fallback"],
  ]);
  assert.deepEqual(Object.keys(lines[0]), ["primary", "exit", "totalSeconds", "status", "host", "inputTokens", "ms", "transitions"]);
  assert.doesNotMatch(stdout + stderr, new RegExp(fakeKey));
});

test("the CLI exits 1 when a run does not return 200", async (t) => {
  const { code, stdout, stderr, lines } = await runCli(t, ["--primary", await closedOrigin(), "--fallback", await closedOrigin()]);
  assert.equal(code, 1);
  assert.equal(lines.length, 1);
  assert.equal(lines[0].status, 502);
  assert.doesNotMatch(stdout + stderr, new RegExp(fakeKey));
});

test("the CLI rejects a run without a primary", async (t) => {
  const { code, lines } = await runCli(t, ["--fallback", "http://127.0.0.1:9"]);
  assert.equal(code, 2);
  assert.deepEqual(lines, []);
});
