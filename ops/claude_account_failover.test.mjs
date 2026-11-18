import assert from "node:assert/strict";
import http from "node:http";
import { once } from "node:events";
import { test } from "node:test";
import { failoverDefaults, isEgressFailure, probeHealth, startAccountProxy } from "./claude_account_launcher.mjs";

// Will's responses when its upstream is unreachable, captured in the unit 0 staging run
// (refused, unresolved name, SYN drop at the default and at tcp_syn_retries=2, first-party dial path).
const egressBodies = {
  refused: '{"type":"error","error":{"type":"api_error","message":"Post \\"https://10.254.239.2:19443/v1/messages?beta=true\\": dial tcp 10.254.239.2:19443: connect: connection refused"}}',
  unresolved: '{"type":"error","error":{"type":"api_error","message":"Post \\"https://missing-unit0-bc-01a1efce.invalid:19443/v1/messages?beta=true\\": dial tcp: lookup missing-unit0-bc-01a1efce.invalid on 127.0.0.11:53: server misbehaving"}}',
  dialTimeout: '{"type":"error","error":{"type":"timeout_error","message":"Post \\"https://10.254.239.2:19444/v1/messages?beta=true\\": dial tcp 10.254.239.2:19444: i/o timeout"}}',
  synDrop: '{"type":"error","error":{"type":"api_error","message":"Post \\"https://10.254.239.2:19444/v1/messages?beta=true\\": dial tcp 10.254.239.2:19444: connect: connection timed out"}}',
  firstParty: '{"type":"error","error":{"type":"api_error","message":"Post \\"https://api.anthropic.com/v1/messages?beta=true\\": claude tls: dial upstream: dial tcp 10.254.239.2:443: connect: connection timed out"}}',
  tokenRefresh: '{"error":{"type":"api_error","message":"auth_unavailable: no auth available; last upstream error: dial tcp: lookup platform.claude.com on 127.0.0.11:53: server misbehaving"}}',
};

const flush = async () => {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setImmediate(resolve));
};

function fakeClock() {
  let now = 0;
  let nextId = 0;
  const timers = new Map();
  const add = (callback, ms, repeat) => {
    const id = ++nextId;
    timers.set(id, { at: now + ms, ms, repeat, callback });
    return id;
  };
  return {
    now: () => now,
    setTimeout: (callback, ms) => add(callback, ms, false),
    clearTimeout: (id) => timers.delete(id),
    setInterval: (callback, ms) => add(callback, ms, true),
    clearInterval: (id) => timers.delete(id),
    pending: () => timers.size,
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        const [id, timer] = due;
        now = timer.at;
        if (timer.repeat) timer.at += timer.ms;
        else timers.delete(id);
        timer.callback();
        await flush();
      }
      now = end;
      await flush();
    },
  };
}

async function listen(t, handler) {
  const server = http.createServer(handler);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => { server.closeAllConnections(); server.close(); });
  return { server, url: `http://127.0.0.1:${server.address().port}` };
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks).toString();
}

// Starts a primary, a fallback and a launcher with an injected clock, probe and transition log.
async function harness(t, { primary, fallback, healthy = { primary: true, fallback: true }, primaryURL, ...failover }) {
  const first = primary ? await listen(t, primary) : null;
  const second = await listen(t, fallback ?? ((_req, res) => res.end("fallback")));
  const clock = fakeClock();
  const transitions = [];
  const probes = [];
  const proxy = await startAccountProxy({
    route: "shared-pool",
    upstream: primaryURL ?? first.url,
    fallbackUpstream: second.url,
    failover: {
      clock,
      log: (entry) => transitions.push(entry),
      probe: async (host) => { probes.push(host); return healthy[host]; },
      ...failover,
    },
  });
  t.after(() => { proxy.server.closeAllConnections(); proxy.server.close(); });
  return { base: proxy.baseURL, clock, transitions, probes, healthy, primary: first, fallback: second };
}

function message(base, path = "/v1/messages?beta=true", model = "claude-opus-5-5") {
  return fetch(base + path, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer test-client-key" },
    body: JSON.stringify({ model, messages: [{ role: "user", content: "test" }], stream: true }),
  });
}

const events = (transitions) => transitions.map((entry) => entry.event);

test("classifies every captured egress-down response as a failover, and nothing else", () => {
  for (const [label, body] of Object.entries(egressBodies)) {
    assert.equal(isEgressFailure(500, body), true, label);
    assert.equal(isEgressFailure(504, body), true, label);
    assert.equal(isEgressFailure(429, body), false, label);
  }
  for (const body of [
    '{"error":{"type":"api_error","message":"auth_unavailable: no auth available"}}',
    '{"error":{"type":"model_cooldown","message":"last upstream error: dial tcp: connection refused"}}',
    '{"error":{"message":"auth_unavailable: invalid_grant: Refresh token not found or invalid"}}',
    '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    '{"error":{"message":"read tcp 10.0.0.2:443: connection reset by peer"}}',
    "not json",
  ]) assert.equal(isEgressFailure(503, body), false, body);
});

for (const [label, egress] of Object.entries(egressBodies)) {
  test(`egress failure (${label}) on the primary replays the same request on the fallback`, async (t) => {
    let primaryCalls = 0;
    const seen = [];
    const { base, transitions } = await harness(t, {
      primary: (_req, res) => {
        primaryCalls++;
        res.writeHead(500, { "content-type": "application/json" });
        res.end(egress);
      },
      fallback: async (req, res) => {
        seen.push({ url: req.url, auth: req.headers.authorization, body: JSON.parse(await readBody(req)) });
        res.writeHead(200, { "content-type": "text/event-stream", "x-cpa-trace-id": "mac" });
        res.end("event: message_stop\ndata: {}\n\n");
      },
    });
    const response = await message(base);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("x-cliproxy-host"), "fallback");
    assert.equal(response.headers.get("x-cpa-trace-id"), "mac");
    assert.match(await response.text(), /message_stop/);
    assert.equal(primaryCalls, 1);
    assert.deepEqual(seen, [{
      url: "/v1/messages?beta=true", auth: "Bearer test-client-key",
      body: { model: "claude-opus-5-5", messages: [{ role: "user", content: "test" }], stream: true },
    }]);
    assert.deepEqual(transitions, [{ event: "egress_hold", seconds: 120 }]);
  });
}

test("an egress hold keeps new requests on the fallback, doubles on repeat, and resets after a primary success", async (t) => {
  let egressDown = true;
  let primaryCalls = 0;
  const { base, clock, transitions } = await harness(t, {
    primary: (_req, res) => {
      primaryCalls++;
      if (egressDown) { res.writeHead(500); res.end(egressBodies.synDrop); } else res.end("primary");
    },
  });
  assert.equal(await (await message(base)).text(), "fallback");
  assert.equal(await (await message(base)).text(), "fallback");
  assert.equal(primaryCalls, 1);
  await clock.advance(120_001);
  assert.equal(await (await message(base)).text(), "fallback");
  assert.equal(primaryCalls, 2);
  await clock.advance(239_000);
  assert.equal(await (await message(base)).text(), "fallback");
  assert.equal(primaryCalls, 2);
  await clock.advance(1_001);
  egressDown = false;
  assert.equal(await (await message(base)).text(), "primary");
  egressDown = true;
  assert.equal(await (await message(base)).text(), "fallback");
  assert.deepEqual(transitions.map((entry) => entry.seconds), [120, 240, 120]);
});

for (const [status, body] of [
  [503, '{"error":{"type":"api_error","message":"auth_unavailable: no auth available"}}'],
  [500, '{"error":{"message":"internal server error"}}'],
  [529, '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'],
  [503, '{"error":{"type":"model_cooldown","message":"last upstream error: dial tcp: connection refused"}}'],
  [401, '{"error":{"message":"unauthorized"}}'],
  [403, '{"error":{"message":"forbidden"}}'],
  [429, '{"error":{"message":"quota exceeded"}}'],
]) test(`passes ${status} ${body} through without switching hosts`, async (t) => {
  let fallbackCalls = 0;
  const { base, transitions } = await harness(t, {
    primary: (_req, res) => { res.writeHead(status, { "retry-after": "42" }); res.end(body); },
    fallback: (_req, res) => { fallbackCalls++; res.end("fallback"); },
  });
  const response = await message(base);
  assert.equal(response.status, status);
  assert.equal(response.headers.get("retry-after"), "42");
  assert.equal(response.headers.get("x-cliproxy-host"), "primary");
  assert.equal(await response.text(), body);
  assert.equal(fallbackCalls, 0);
  assert.deepEqual(transitions, []);
});

test("a refused primary fails over, then returns after two passing probes and at least 30 s", async (t) => {
  const { base, clock, transitions, healthy, primary } = await harness(t, { primary: (_req, res) => res.end("primary") });
  const primaryURL = primary.url;
  await new Promise((resolve) => { primary.server.closeAllConnections(); primary.server.close(resolve); });
  const response = await message(base);
  assert.equal(response.headers.get("x-cliproxy-host"), "fallback");
  assert.equal(await response.text(), "fallback");
  assert.deepEqual(transitions, [{ event: "primary_down", reason: "ECONNREFUSED" }]);

  const revived = http.createServer((_req, res) => res.end("primary"));
  revived.listen(Number(new URL(primaryURL).port), "127.0.0.1");
  await once(revived, "listening");
  t.after(() => { revived.closeAllConnections(); revived.close(); });
  healthy.primary = false;
  await clock.advance(15_000);
  healthy.primary = true;
  await clock.advance(15_000);
  assert.equal(await (await message(base)).text(), "fallback", "one passing probe is not enough");
  await clock.advance(15_000);
  assert.deepEqual(events(transitions), ["primary_down", "primary_up"]);
  assert.equal(await (await message(base)).text(), "primary");
});

test("recovery waits for the minimum time even when probes pass sooner", async (t) => {
  const { base, clock, transitions, primary } = await harness(t, {
    primary: (_req, res) => res.end("primary"),
    settings: { recoveryIntervalMs: 5_000 },
  });
  const port = Number(new URL(primary.url).port);
  await new Promise((resolve) => { primary.server.closeAllConnections(); primary.server.close(resolve); });
  assert.equal(await (await message(base)).text(), "fallback");
  const revived = http.createServer((_req, res) => res.end("primary"));
  revived.listen(port, "127.0.0.1");
  await once(revived, "listening");
  t.after(() => { revived.closeAllConnections(); revived.close(); });
  await clock.advance(25_000);
  assert.deepEqual(events(transitions), ["primary_down"], "five passing probes in 25 s are not enough");
  await clock.advance(5_000);
  assert.deepEqual(events(transitions), ["primary_down", "primary_up"]);
  assert.equal(await (await message(base)).text(), "primary");
});

test("the connect guard fails over when the primary never finishes connecting", async (t) => {
  const { base, clock, transitions } = await harness(t, {
    primaryURL: "http://primary.unit-test:8317",
    // Never answering the lookup keeps the socket connecting, like a dropped SYN.
    lookup: (hostname, options, callback) => {
      if (hostname !== "primary.unit-test") return void import("node:dns").then((dns) => dns.lookup(hostname, options, callback));
    },
  });
  const pending = message(base);
  await flush();
  await clock.advance(failoverDefaults.connectTimeoutMs);
  const response = await pending;
  assert.equal(response.headers.get("x-cliproxy-host"), "fallback");
  assert.equal(await response.text(), "fallback");
  assert.deepEqual(transitions, [{ event: "primary_down", reason: "connect_timeout" }]);
});

test("a message reset after connect with no bytes returns a retryable 502 and is not replayed", async (t) => {
  let fallbackCalls = 0;
  const { base, transitions, probes, healthy } = await harness(t, {
    primary: (req) => req.socket.destroy(),
    fallback: (_req, res) => { fallbackCalls++; res.end("fallback"); },
  });
  healthy.primary = false;
  const response = await message(base);
  assert.equal(response.status, 502);
  assert.equal(response.headers.get("x-should-retry"), "true");
  await response.text();
  assert.equal(fallbackCalls, 0);
  assert.deepEqual(probes, ["primary", "primary"], "startup probe, then one probe after the reset");
  assert.deepEqual(transitions, [{ event: "primary_down", reason: "reset" }]);
  assert.equal(await (await message(base)).text(), "fallback", "the client's retry takes the fallback");
});

test("a reset with a healthy primary keeps the primary in service", async (t) => {
  let resets = 1;
  const { base, transitions } = await harness(t, {
    primary: (req, res) => { if (resets-- > 0) req.socket.destroy(); else res.end("primary"); },
  });
  assert.equal((await message(base)).status, 502);
  assert.equal(await (await message(base)).text(), "primary");
  assert.deepEqual(transitions, []);
});

test("count_tokens and the model list replay on the fallback after a zero-byte reset", async (t) => {
  const { base } = await harness(t, {
    primary: (req) => req.socket.destroy(),
    fallback: (req, res) => {
      if (req.url === "/v1/models") return res.end(JSON.stringify({ data: [{ id: "claude-opus-5-5" }, { id: "t3-gmail/claude-opus-5-5" }] }));
      res.end('{"input_tokens":3}');
    },
  });
  const counted = await message(base, "/v1/messages/count_tokens");
  assert.equal(counted.status, 200);
  assert.equal(counted.headers.get("x-cliproxy-host"), "fallback");
  assert.deepEqual(await counted.json(), { input_tokens: 3 });
  const models = await fetch(base + "/v1/models");
  assert.equal(models.headers.get("x-cliproxy-host"), "fallback");
  assert.deepEqual((await models.json()).data, [{ id: "claude-opus-5-5" }]);
});

test("a drop after streaming starts destroys the client response without replay", async (t) => {
  let upstream;
  let fallbackCalls = 0;
  const { base } = await harness(t, {
    primary: (_req, res) => {
      upstream = res;
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write("event: message_start\ndata: {}\n\n");
    },
    fallback: (_req, res) => { fallbackCalls++; res.end(); },
  });
  const response = await message(base);
  const reader = response.body.getReader();
  assert.match(new TextDecoder().decode((await reader.read()).value), /message_start/);
  upstream.socket.destroy();
  await assert.rejects(reader.read());
  assert.equal(fallbackCalls, 0);
});

test("a stream that keeps delivering is never cut, even while every probe fails", async (t) => {
  let upstream;
  const { base, clock, healthy, transitions } = await harness(t, {
    primary: (_req, res) => {
      upstream = res;
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write("event: message_start\ndata: {}\n\n");
    },
  });
  const response = await message(base);
  const reader = response.body.getReader();
  await reader.read();
  healthy.primary = false;
  for (let i = 0; i < 5; i++) {
    upstream.write(`event: ping\ndata: ${i}\n\n`);
    await reader.read();
    await clock.advance(failoverDefaults.livenessIntervalMs);
  }
  upstream.end("event: message_stop\ndata: {}\n\n");
  let rest = "";
  for (let chunk = await reader.read(); !chunk.done; chunk = await reader.read()) rest += new TextDecoder().decode(chunk.value);
  assert.match(rest, /message_stop/);
  assert.deepEqual(transitions, [{ event: "primary_down", reason: "liveness" }], "new requests leave the host");
  assert.equal(await (await message(base)).text(), "fallback");
});

test("a silent request is cut after three failed probes", async (t) => {
  let arrived;
  const received = new Promise((resolve) => { arrived = resolve; });
  const { base, clock, healthy, transitions } = await harness(t, { primary: () => arrived() });
  const pending = message(base);
  await received;
  healthy.primary = false;
  await clock.advance(failoverDefaults.livenessIntervalMs * 2);
  await clock.advance(failoverDefaults.livenessIntervalMs);
  const response = await pending;
  assert.equal(response.status, 502);
  assert.equal(response.headers.get("x-should-retry"), "true");
  await response.text();
  assert.deepEqual(events(transitions), ["primary_down", "request_cut"]);
});

test("a silent request on a healthy host is never cut", async (t) => {
  let upstream;
  let arrived;
  const received = new Promise((resolve) => { arrived = resolve; });
  const { base, clock, transitions } = await harness(t, { primary: (_req, res) => { upstream = res; arrived(); } });
  const pending = message(base);
  await received;
  await clock.advance(failoverDefaults.livenessIntervalMs * 10);
  upstream.end("slow but fine");
  assert.equal(await (await pending).text(), "slow but fine");
  assert.deepEqual(transitions, []);
});

test("if the sticky fallback fails before connecting, the primary is tried once", async (t) => {
  let primaryCalls = 0;
  const { base, fallback } = await harness(t, {
    primary: (_req, res) => {
      primaryCalls++;
      if (primaryCalls === 1) { res.writeHead(500); res.end(egressBodies.refused); } else res.end("primary");
    },
  });
  assert.equal(await (await message(base)).text(), "fallback");
  await new Promise((resolve) => { fallback.server.closeAllConnections(); fallback.server.close(resolve); });
  const response = await message(base);
  assert.equal(response.headers.get("x-cliproxy-host"), "primary");
  assert.equal(await response.text(), "primary");
  assert.equal(primaryCalls, 2);
});

test("a failed startup probe sends the first request to the fallback", async (t) => {
  let primaryCalls = 0;
  const { base, transitions } = await harness(t, {
    primary: (_req, res) => { primaryCalls++; res.end("primary"); },
    probe: async () => false,
  });
  assert.equal(await (await message(base)).text(), "fallback");
  assert.equal(primaryCalls, 0);
  assert.deepEqual(transitions, [{ event: "primary_down", reason: "startup_probe" }]);
});

test("without a fallback the launcher starts no timers and sends no probes", async (t) => {
  const used = [];
  const spy = new Proxy({}, { get: (_target, name) => () => { used.push(name); return 0; } });
  const upstream = await listen(t, (_req, res) => res.end("primary"));
  const proxy = await startAccountProxy({
    route: "shared-pool", upstream: upstream.url,
    failover: { clock: spy, probe: async () => { used.push("probe"); return true; }, log: () => used.push("log") },
  });
  t.after(() => { proxy.server.closeAllConnections(); proxy.server.close(); });
  const response = await message(proxy.baseURL);
  assert.equal(await response.text(), "primary");
  assert.equal(response.headers.get("x-cliproxy-host"), null);
  assert.deepEqual(used, []);
});

test("fallback configuration is limited to a distinct origin on the shared pool", async () => {
  await assert.rejects(startAccountProxy({ prefix: "t3-gmail", upstream: "http://127.0.0.1:1", fallbackUpstream: "http://127.0.0.1:2" }), /shared-pool route/);
  await assert.rejects(startAccountProxy({ route: "shared-pool", upstream: "http://127.0.0.1:1", fallbackUpstream: "http://127.0.0.1:1/" }), /different CLIProxy host/);
  await assert.rejects(startAccountProxy({ route: "shared-pool", upstream: "http://127.0.0.1:1", fallbackUpstream: "http://user:pw@127.0.0.1:2" }), /HTTP origin/);
});

test("the health probe needs a 200 with status ok, not just an answer", async (t) => {
  const answers = [[200, '{"status":"ok"}'], [200, "ok"], [503, '{"status":"ok"}'], [200, '{"status":"starting"}']];
  let index = 0;
  const server = await listen(t, (_req, res) => { const [status, body] = answers[index++]; res.writeHead(status); res.end(body); });
  const origin = new URL(server.url);
  assert.deepEqual(
    [await probeHealth(origin), await probeHealth(origin), await probeHealth(origin), await probeHealth(origin)],
    [true, false, false, false],
  );
  const closed = await listen(t, () => {});
  await new Promise((resolve) => { closed.server.close(resolve); });
  assert.equal(await probeHealth(new URL(closed.url)), false);
});
