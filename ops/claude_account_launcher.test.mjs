import assert from "node:assert/strict";
import http from "node:http";
import { once } from "node:events";
import { test } from "node:test";
import { startAccountProxy } from "./claude_account_launcher.mjs";

async function fixture(t, handler, prefix = "icloud") {
  const upstream = http.createServer(handler);
  upstream.listen(0, "127.0.0.1");
  await once(upstream, "listening");
  const proxy = await startAccountProxy({ prefix, upstream: `http://127.0.0.1:${upstream.address().port}` });
  t.after(() => { proxy.server.closeAllConnections(); proxy.server.close(); upstream.closeAllConnections(); upstream.close(); });
  return proxy.baseURL;
}

async function readJSON(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks));
}

test("pins every message and token-count request, preserving beta queries and auth", async (t) => {
  const calls = [];
  const base = await fixture(t, async (req, res) => {
    calls.push({ path: req.url, auth: req.headers.authorization, body: await readJSON(req) });
    res.end('{}');
  });
  for (const [path, model] of [["/v1/messages?beta=true", "claude-sonnet-5"], ["/v1/messages/count_tokens", "icloud/claude-haiku-4-5-20251001"]]) {
    const res = await fetch(base + path, { method: "POST", headers: { "content-type": "application/json", authorization: "Bearer test-key" }, body: JSON.stringify({ model, messages: [] }) });
    assert.equal(res.status, 200);
    await res.text();
  }
  assert.deepEqual(calls.map((x) => [x.path, x.auth, x.body.model]), [
    ["/v1/messages?beta=true", "Bearer test-key", "icloud/claude-sonnet-5"],
    ["/v1/messages/count_tokens", "Bearer test-key", "icloud/claude-haiku-4-5-20251001"],
  ]);
});

test("rejects another account and malformed requests before contacting the pool", async (t) => {
  let called = 0;
  const base = await fixture(t, (_req, res) => { called++; res.end(); });
  for (const body of ['{"model":"gmail/claude-sonnet-5"}', '{}', 'invalid']) {
    const res = await fetch(base + "/v1/messages", { method: "POST", headers: { "content-type": "application/json" }, body });
    assert.equal(res.status, 400);
    await res.text();
  }
  assert.equal(called, 0);
});

test("returns chosen account cooldown without retrying another account", async (t) => {
  let calls = 0;
  const base = await fixture(t, async (req, res) => {
    calls++;
    assert.equal((await readJSON(req)).model, "gmail/claude-sonnet-5");
    res.writeHead(429, { "retry-after": "3600", "content-type": "application/json" });
    res.end('{"error":{"type":"rate_limit_error"}}');
  }, "gmail");
  const res = await fetch(base + "/v1/messages", { method: "POST", headers: { "content-type": "application/json" }, body: '{"model":"claude-sonnet-5"}' });
  assert.equal(res.status, 429);
  assert.equal(res.headers.get("retry-after"), "3600");
  assert.equal((await res.json()).error.type, "rate_limit_error");
  assert.equal(calls, 1);
});

test("forwards SSE incrementally and keeps account trace headers", async (t) => {
  let finish;
  const base = await fixture(t, async (req, res) => {
    await readJSON(req);
    res.writeHead(200, { "content-type": "text/event-stream", "x-cpa-trace-id": "icloud-proof" });
    finish = () => res.end('event: message_stop\ndata: {}\n\n');
    res.write('event: message_start\ndata: {}\n\n');
  });
  const res = await fetch(base + "/v1/messages", { method: "POST", headers: { "content-type": "application/json" }, body: '{"model":"claude-sonnet-5","stream":true}' });
  assert.equal(res.headers.get("x-cpa-trace-id"), "icloud-proof");
  const reader = res.body.getReader();
  const first = await reader.read();
  assert.match(new TextDecoder().decode(first.value), /message_start/);
  finish();
  let rest = "";
  for (;;) { const part = await reader.read(); if (part.done) break; rest += new TextDecoder().decode(part.value); }
  assert.match(rest, /message_stop/);
});

test("model discovery exposes only the chosen account with normal model IDs", async (t) => {
  const base = await fixture(t, (_req, res) => {
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ data: [{ id: "icloud/claude-sonnet-5" }, { id: "gmail/claude-sonnet-5" }, { id: "claude-sonnet-5" }] }));
  });
  const res = await fetch(base + "/v1/models");
  assert.deepEqual((await res.json()).data, [{ id: "claude-sonnet-5" }]);
});
