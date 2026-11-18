#!/usr/bin/env node
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";

const hopHeaders = new Set([
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
]);

const supportedOperations = new Set([
  "POST /v1/messages", "POST /v1/messages/count_tokens", "GET /v1/models",
]);

const childShutdownGraceMs = 5000;
const maxCooldownBodyBytes = 64 * 1024;
// Claude Code's own retries span about 160 s and CLIProxy holds a request for up to 75 s,
// so cooldowns up to 180 s are absorbed; only longer ones should end the turn.
const noRetryCooldownSeconds = 180;

// Host failover applies only when a fallback origin is configured (ops/FLEET_DESIGN.md sections 6 and 7).
export const failoverDefaults = Object.freeze({
  connectTimeoutMs: 3000,
  probeTimeoutMs: 3000,
  livenessIntervalMs: 20_000,
  livenessFailures: 3,
  recoveryIntervalMs: 15_000,
  recoverySuccesses: 2,
  recoveryMinimumMs: 30_000,
  egressHoldMs: 120_000,
  egressHoldMaxMs: 600_000,
});
const maxFailoverErrorBytes = 64 * 1024;
const maxHealthBodyBytes = 4096;
const launcherPath = process.argv[1] && path.resolve(process.argv[1]);
const launcherSource = fileURLToPath(import.meta.url);
const generationDirectory = `${path.sep}.fleet-releases${path.sep}`;
const launcherOps = launcherSource.includes(generationDirectory)
  ? launcherSource.split(generationDirectory, 1)[0]
  : path.dirname(launcherSource);
const instanceOps = launcherOps === path.join(os.homedir(), "cliproxy-live/ops")
  ? path.join(os.homedir(), "cliproxy/ops")
  : launcherOps;
const defaultFailoverLog = path.join(instanceOps, "runtime/fleet/failover.jsonl");
const accountOrQuotaError = /invalid_grant|unauthorized|rate_limit_error|usage_limit_reached|quota_exceeded|model_cooldown/i;
// Only dial errors: a read or write error means an earlier connection to the upstream existed.
const dialError = /\bdial (?:tcp|udp)[46]?\b|\bdial upstream:/i;

// A 5xx whose error says CLIProxy could not dial its upstream means its last attempt never connected,
// so the request is replayed on the other host. An earlier retry round may still have reached the
// provider when egress fails mid-request. Claude Code would usually resend the request after this 5xx
// anyway, and the replay sends that retry to the host that can serve it; only when Claude Code had no
// retries left does the replay add a run.
export function isEgressFailure(statusCode, body) {
  if (statusCode < 500) return false;
  let error;
  try {
    error = JSON.parse(body)?.error;
  } catch {
    return false;
  }
  const message = typeof error === "string" ? error : error?.message;
  if (typeof message !== "string" || accountOrQuotaError.test([error?.type, error?.code, message].join(" "))) return false;
  return dialError.test(message);
}

function requestOptions(origin, method, url, headers) {
  return {
    protocol: origin.protocol, hostname: origin.hostname, port: origin.port,
    method, path: `${origin.pathname.replace(/\/$/, "")}${url}`, headers,
  };
}

// GET /healthz must return 200 with {"status":"ok"}; a TCP accept alone proves nothing.
export function probeHealth(origin, timeoutMs = failoverDefaults.probeTimeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (healthy) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      probe.destroy();
      resolve(healthy);
    };
    const transport = origin.protocol === "https:" ? https : http;
    const probe = transport.request({
      ...requestOptions(origin, "GET", "/healthz", { accept: "application/json" }), path: "/healthz", agent: false,
    }, (response) => {
      const chunks = [];
      let size = 0;
      response.on("data", (chunk) => {
        size += chunk.length;
        if (size > maxHealthBodyBytes) return finish(false);
        chunks.push(chunk);
      });
      response.on("end", () => {
        try {
          finish(response.statusCode === 200 && JSON.parse(Buffer.concat(chunks)).status === "ok");
        } catch {
          finish(false);
        }
      });
      response.on("error", () => finish(false));
    });
    const timer = setTimeout(() => finish(false), timeoutMs);
    timer.unref?.();
    probe.on("error", () => finish(false));
    probe.end();
  });
}

// Monotonic, so a wall-clock step cannot shift a hold, a recovery or a liveness window.
const realClock = {
  now: () => performance.now(),
  setTimeout: (callback, ms) => { const timer = setTimeout(callback, ms); timer.unref?.(); return timer; },
  clearTimeout: (timer) => clearTimeout(timer),
  setInterval: (callback, ms) => { const timer = setInterval(callback, ms); timer.unref?.(); return timer; },
  clearInterval: (timer) => clearInterval(timer),
};

// One JSON line per host transition. It holds no request bodies, URLs or keys.
function transitionLog(file) {
  return (entry) => {
    const line = `${JSON.stringify({ ts: new Date().toISOString(), pid: process.pid, ...entry })}\n`;
    fs.promises.mkdir(path.dirname(file), { recursive: true, mode: 0o700 })
      .then(() => fs.promises.appendFile(file, line, { mode: 0o600 }))
      .catch(() => {});
  };
}

// Chooses the host for new requests. The primary leaves service on a failed connect or health check
// and returns after enough passing probes; an egress failure holds new requests on the fallback instead,
// because the primary's own health check stays green while its upstream is unreachable.
function createHostSelector({ clock, probe, log, settings, onRecovered }) {
  let down = null;
  let holdUntil = 0;
  let holdMs = settings.egressHoldMs;

  function markDown(reason) {
    if (down) return;
    const state = { ticks: 0, successes: 0, probing: false };
    down = state;
    state.timer = clock.setInterval(async () => {
      // Elapsed time is counted in ticks, so a timer that fires slightly early cannot delay recovery.
      state.ticks++;
      if (state.probing) return;
      state.probing = true;
      const healthy = await probe("primary");
      state.probing = false;
      if (down !== state) return;
      state.successes = healthy ? state.successes + 1 : 0;
      if (state.successes >= settings.recoverySuccesses && state.ticks * settings.recoveryIntervalMs >= settings.recoveryMinimumMs) {
        clock.clearInterval(state.timer);
        down = null;
        onRecovered();
        log({ event: "primary_up" });
      }
    }, settings.recoveryIntervalMs);
    log({ event: "primary_down", reason });
  }

  return {
    choose: () => (down || clock.now() < holdUntil ? "fallback" : "primary"),
    markDown,
    egressDown() {
      // Concurrent requests that hit the same outage share one hold.
      if (clock.now() < holdUntil) return;
      holdUntil = clock.now() + holdMs;
      log({ event: "egress_hold", seconds: holdMs / 1000 });
      holdMs = Math.min(holdMs * 2, settings.egressHoldMaxMs);
    },
    primaryAnswered() {
      holdMs = settings.egressHoldMs;
    },
    stop() {
      if (down) clock.clearInterval(down.timer);
      down = null;
    },
  };
}

// While requests are open, probes their hosts. A request is ended only when its host failed
// `livenessFailures` probes in a row and the request received no bytes over that whole window,
// so a stream that is still delivering is never cut.
function createLivenessWatch({ clock, probe, settings, onHostFailing }) {
  const watched = new Set();
  const hostFailures = new Map();
  let timer = null;
  let ticking = false;
  // Requests that joined after this moment have not been watched for a whole interval yet.
  let windowStart = 0;

  async function tick() {
    if (ticking) return;
    ticking = true;
    const generation = timer;
    const counted = windowStart;
    windowStart = clock.now();
    try {
      const hosts = [...new Set([...watched].map((entry) => entry.host))];
      const healthy = new Map(await Promise.all(hosts.map(async (host) => [host, await probe(host)])));
      // The watch stopped (and maybe restarted) while this tick waited; its results are stale.
      if (timer !== generation) return;
      for (const host of hostFailures.keys()) if (!healthy.has(host)) hostFailures.delete(host);
      for (const host of hosts) {
        const failures = healthy.get(host) ? 0 : (hostFailures.get(host) ?? 0) + 1;
        hostFailures.set(host, failures);
        if (failures >= settings.livenessFailures) onHostFailing(host);
      }
      for (const entry of [...watched]) {
        if (!healthy.has(entry.host) || entry.joined > counted) continue;
        const bytes = entry.socket.bytesRead;
        entry.silent = healthy.get(entry.host) || bytes !== entry.bytes ? 0 : entry.silent + 1;
        entry.bytes = bytes;
        if (entry.silent >= settings.livenessFailures) entry.cut();
      }
    } finally {
      ticking = false;
    }
  }

  function stop() {
    if (timer !== null) clock.clearInterval(timer);
    timer = null;
    hostFailures.clear();
  }

  return {
    add(entry) {
      watched.add(entry);
      if (timer === null) {
        windowStart = clock.now();
        timer = clock.setInterval(tick, settings.livenessIntervalMs);
      }
      return () => {
        watched.delete(entry);
        if (!watched.size) stop();
      };
    },
    // Called when the host passed its recovery probes, which breaks every failure run on it.
    forget(host) {
      hostFailures.delete(host);
      for (const entry of watched) if (entry.host === host) entry.silent = 0;
    },
    stop() {
      watched.clear();
      stop();
    },
  };
}

function forwardedHeaders(headers) {
  const excluded = new Set(hopHeaders);
  for (const name of (headers.connection ?? "").split(",")) excluded.add(name.trim().toLowerCase());
  return Object.fromEntries(Object.entries(headers).filter(([name]) => !excluded.has(name)));
}

function fail(response, status, message) {
  if (response.headersSent) return response.destroy();
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify({ type: "error", error: { type: "api_error", message } }));
}

async function requestBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 64 * 1024 * 1024) throw new Error("Request exceeds 64 MiB.");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function isLongCooldown(body) {
  let payload;
  try {
    payload = JSON.parse(body);
  } catch {
    return false;
  }
  const error = payload?.error;
  if (error?.code === "model_cooldown") {
    return Number.isFinite(error.reset_seconds) && error.reset_seconds > noRetryCooldownSeconds;
  }
  if (payload?.type !== "error" || error?.type !== "rate_limit_error" || typeof error.message !== "string") {
    return false;
  }
  // The Claude handler renders the selector's reset as a Go duration at the end of its message.
  const reset = / \(reset after (?:(\d+)h)?(?:(\d+)m)?(\d+)s\)$/.exec(error.message);
  if (!reset) return false;
  const [, hours = "0", minutes = "0", seconds] = reset;
  const totalSeconds = Number(hours) * 3600 + Number(minutes) * 60 + Number(seconds);
  return Number.isFinite(totalSeconds) && totalSeconds > noRetryCooldownSeconds;
}

function forwardUpstreamResponse(upstream, response, outgoing) {
  const { statusCode, headers } = upstream;
  upstream.on("error", () => response.destroy());

  function stream(prefix) {
    response.writeHead(statusCode, outgoing);
    if (prefix?.length) response.write(prefix);
    upstream.pipe(response);
  }

  if (statusCode !== 429 || headers["retry-after"] !== undefined ||
      !/^\s*application\/json\s*(;|$)/i.test(headers["content-type"] ?? "") ||
      !/^identity$/i.test(headers["content-encoding"] ?? "identity") ||
      Number(headers["content-length"]) > maxCooldownBodyBytes) {
    return stream();
  }

  const chunks = [];
  let size = 0;
  function read() {
    const available = upstream.readableLength;
    if (size + available > maxCooldownBodyBytes) {
      upstream.off("readable", read);
      upstream.off("end", finish);
      stream(Buffer.concat(chunks, size));
      return;
    }
    const chunk = upstream.read(available);
    // An exact-size read can drain the final chunk without emitting "end".
    if (available > 0) upstream.read(0);
    if (chunk === null) return;
    chunks.push(chunk);
    size += chunk.length;
  }

  function finish() {
    const body = Buffer.concat(chunks, size);
    if (isLongCooldown(body)) outgoing["x-should-retry"] = "false";
    response.writeHead(statusCode, outgoing);
    response.end(body);
  }

  upstream.on("readable", read);
  upstream.once("end", finish);
}

function isHttpOrigin(origin) {
  return ["http:", "https:"].includes(origin.protocol) && !origin.username && !origin.password && !origin.search && !origin.hash;
}

export async function startAccountProxy({ route = "account", prefix, upstream, fallbackUpstream, failover = {} }) {
  if (route !== "account" && route !== "shared-pool") throw new Error("CLIPROXY_CLAUDE_ROUTE must be account or shared-pool.");
  if (route === "shared-pool" && prefix) throw new Error("The shared-pool route must not set CLIPROXY_ACCOUNT_PREFIX.");
  if (route === "account" && !/^[a-zA-Z0-9_-]+$/.test(prefix ?? "")) throw new Error("A valid CLIPROXY_ACCOUNT_PREFIX is required.");
  const target = new URL(upstream);
  if (!isHttpOrigin(target)) {
    throw new Error("CLIPROXY_BASE_URL must be an HTTP origin without credentials or query parameters.");
  }
  const fallback = fallbackUpstream ? new URL(fallbackUpstream) : null;
  if (fallback) {
    // A named account never crosses hosts: only the shared pool is the same on both.
    if (route !== "shared-pool") throw new Error("CLIPROXY_FALLBACK_URL is only supported on the shared-pool route.");
    if (!isHttpOrigin(fallback)) throw new Error("CLIPROXY_FALLBACK_URL must be an HTTP origin without credentials or query parameters.");
    if (fallback.origin === target.origin) throw new Error("CLIPROXY_FALLBACK_URL must name a different CLIProxy host.");
  }
  const transport = target.protocol === "https:" ? https : http;

  const deliver = (request, path, response, upstreamResponse) => {
    const outgoing = forwardedHeaders(upstreamResponse.headers);
    if (request.method === "GET" && path === "/v1/models" && upstreamResponse.statusCode === 200) {
      (async () => {
        try {
          const chunks = [];
          for await (const chunk of upstreamResponse) chunks.push(chunk);
          const value = JSON.parse(Buffer.concat(chunks));
          value.data = route === "shared-pool"
            ? value.data.filter((model) => model.id.startsWith("claude-") && !model.id.includes("/"))
            : value.data.filter((model) => model.id.startsWith(`${prefix}/`)).map((model) => ({
              ...model, id: model.id.slice(prefix.length + 1),
            }));
          response.writeHead(200, outgoing);
          response.end(JSON.stringify(value));
        } catch {
          fail(response, 502, "The proxy returned an invalid model list.");
        }
      })();
      return;
    }
    forwardUpstreamResponse(upstreamResponse, response, outgoing);
  };

  const failoverRoute = fallback && createFailoverRoute({ target, fallback, deliver, options: failover });
  const server = http.createServer(async (request, response) => {
    let body;
    const path = request.url.split("?")[0];
    if (!supportedOperations.has(`${request.method} ${path}`)) {
      return fail(response, 404, "This request is not supported by the account launcher.");
    }
    try {
      if (request.method === "POST") {
        if (request.headers["content-encoding"] && request.headers["content-encoding"] !== "identity") {
          return fail(response, 415, "Compressed requests are not supported by the account launcher.");
        }
        if (!/^\s*application\/json\s*(;|$)/i.test(request.headers["content-type"] ?? "")) {
          return fail(response, 415, "A Claude request requires an application/json body.");
        }
        const value = JSON.parse(await requestBody(request));
        if (value === null || typeof value !== "object" || Array.isArray(value) || typeof value.model !== "string" || !value.model) {
          return fail(response, 400, "A model is required for a Claude request.");
        }
        if (route === "shared-pool") {
          if (!value.model.startsWith("claude-") || value.model.includes("/")) {
            return fail(response, 400, "The shared Claude pool requires an unprefixed Claude model.");
          }
        } else {
          if (value.model.includes("/") && !value.model.startsWith(`${prefix}/`)) {
            return fail(response, 400, "This Claude entry is pinned to a different account.");
          }
          if (!value.model.startsWith(`${prefix}/`)) value.model = `${prefix}/${value.model}`;
        }
        body = Buffer.from(JSON.stringify(value));
      } else {
        body = await requestBody(request);
      }
    } catch {
      return fail(response, 400, "The account launcher could not read the JSON request.");
    }

    const headers = forwardedHeaders(request.headers);
    headers["accept-encoding"] = "identity";
    if (body.length) headers["content-length"] = String(body.length);
    if (failoverRoute) return failoverRoute.forward(request, path, response, body, headers);
    const upstreamRequest = transport.request(requestOptions(target, request.method, request.url, headers), (upstreamResponse) => {
      deliver(request, path, response, upstreamResponse);
    });
    upstreamRequest.on("error", () => fail(response, 502, unavailableMessage));
    response.on("close", () => upstreamRequest.destroy());
    upstreamRequest.end(body);
  });
  if (failoverRoute) {
    server.once("close", failoverRoute.stop);
    await failoverRoute.start();
  }
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return { server, baseURL: `http://127.0.0.1:${server.address().port}` };
}

const unavailableMessage = "CLIProxy is unavailable for the selected Claude route.";

function retryableFailure(response, message) {
  if (!response.headersSent) response.setHeader("x-should-retry", "true");
  fail(response, 502, message);
}

function createFailoverRoute({ target, fallback, deliver, options }) {
  const settings = { ...failoverDefaults, ...options.settings };
  const clock = options.clock ?? realClock;
  const log = options.log ?? transitionLog(defaultFailoverLog);
  const origins = { primary: target, fallback };
  const probe = options.probe ?? ((host) => probeHealth(origins[host], settings.probeTimeoutMs));
  const selector = createHostSelector({ clock, probe, log, settings, onRecovered: () => watch.forget("primary") });
  const watch = createLivenessWatch({
    clock, probe, settings, onHostFailing: (host) => { if (host === "primary") selector.markDown("liveness"); },
  });
  const other = (host) => (host === "primary" ? "fallback" : "primary");

  function forward(request, path, response, body, headers) {
    // count_tokens and the model list are safe to replay; a message may already be generating upstream.
    const replayable = !(request.method === "POST" && path === "/v1/messages");
    let failedOver = false;
    let active = null;
    response.once("close", () => active?.destroy());

    const switchHost = (host) => {
      if (failedOver) return false;
      failedOver = true;
      send(other(host));
      return true;
    };

    const send = (host) => {
      if (response.destroyed || response.writableEnded) return;
      const origin = origins[host];
      response.setHeader("x-cliproxy-host", host);
      let socket = null;
      let connected = false;
      let guard = null;
      let release = () => {};
      const upstreamRequest = (origin.protocol === "https:" ? https : http).request({
        ...requestOptions(origin, request.method, request.url, headers),
        agent: false,
        ...(options.lookup ? { lookup: options.lookup } : {}),
      }, (upstreamResponse) => {
        if (active !== upstreamRequest || response.destroyed) return void upstreamResponse.destroy();
        if (host === "primary" && upstreamResponse.statusCode >= 500 && !failedOver) {
          return classifyPrimaryError(upstreamRequest, upstreamResponse);
        }
        if (host === "primary" && upstreamResponse.statusCode < 400) selector.primaryAnswered();
        deliver(request, path, response, upstreamResponse);
      });
      active = upstreamRequest;
      upstreamRequest.once("socket", (assigned) => {
        socket = assigned;
        if (assigned.connecting) {
          guard = clock.setTimeout(() => {
            upstreamRequest.destroy(Object.assign(new Error("connect timeout"), { code: "ECONNECTGUARD" }));
          }, settings.connectTimeoutMs);
          assigned.once("connect", () => {
            connected = true;
            clock.clearTimeout(guard);
            guard = null;
          });
        } else {
          connected = true;
        }
        release = watch.add({
          host, socket: assigned, bytes: assigned.bytesRead, silent: 0, joined: clock.now(),
          cut() {
            if (active !== upstreamRequest) return;
            active = null;
            log({ event: "request_cut", host });
            if (response.headersSent) response.destroy();
            else retryableFailure(response, "CLIProxy stopped answering; retry the request.");
            upstreamRequest.destroy();
          },
        });
      });
      upstreamRequest.once("close", () => {
        if (guard !== null) clock.clearTimeout(guard);
        release();
      });
      upstreamRequest.on("error", (error) => {
        if (active !== upstreamRequest || response.destroyed) return;
        active = null;
        if (!connected) {
          // Nothing reached the host, so any request may be sent to the other one.
          if (host === "primary") selector.markDown(error.code === "ECONNECTGUARD" ? "connect_timeout" : error.code ?? "connect_error");
          if (!switchHost(host)) fail(response, 502, unavailableMessage);
          return;
        }
        if (response.headersSent) return void response.destroy();
        if ((socket?.bytesRead ?? 0) > 0) return void fail(response, 502, unavailableMessage);
        // Closed after connect with nothing received: check the host once before deciding.
        const healthy = probe(host).then((ok) => {
          if (!ok && host === "primary") selector.markDown("reset");
          return ok;
        });
        if (replayable && switchHost(host)) return;
        healthy.then(() => {
          if (!response.destroyed && !response.writableEnded) {
            retryableFailure(response, "CLIProxy closed the connection before replying; retry the request.");
          }
        });
      });
      upstreamRequest.end(body);
    };

    const classifyPrimaryError = (upstreamRequest, upstreamResponse) => {
      const chunks = [];
      let size = 0;
      upstreamResponse.on("error", () => { if (active === upstreamRequest) response.destroy(); });
      const passThrough = () => response.writeHead(upstreamResponse.statusCode, forwardedHeaders(upstreamResponse.headers));
      const collect = (chunk) => {
        chunks.push(chunk);
        size += chunk.length;
        if (size > maxFailoverErrorBytes) {
          upstreamResponse.off("data", collect);
          upstreamResponse.off("end", complete);
          passThrough();
          response.write(Buffer.concat(chunks));
          upstreamResponse.pipe(response);
        }
      };
      const complete = () => {
        if (active !== upstreamRequest || response.destroyed) return;
        const errorBody = Buffer.concat(chunks);
        if (isEgressFailure(upstreamResponse.statusCode, errorBody)) {
          selector.egressDown();
          active = null;
          switchHost("primary");
          return;
        }
        passThrough();
        response.end(errorBody);
      };
      upstreamResponse.on("data", collect);
      upstreamResponse.on("end", complete);
    };

    send(selector.choose());
  }

  return {
    forward,
    async start() {
      if (!(await probe("primary"))) selector.markDown("startup_probe");
    },
    stop() {
      selector.stop();
      watch.stop();
    },
  };
}

function connectTimeoutSetting(value) {
  if (value === undefined || value === "") return {};
  const ms = Number(value);
  if (!Number.isInteger(ms) || ms < 100 || ms > 60_000) throw new Error("CLIPROXY_CONNECT_TIMEOUT_MS must be an integer from 100 to 60000.");
  return { connectTimeoutMs: ms };
}

async function main() {
  const binary = process.env.CLIPROXY_CLAUDE_BINARY;
  if (!binary) throw new Error("CLIPROXY_CLAUDE_BINARY is required.");
  const fallbackUpstream = process.env.CLIPROXY_FALLBACK_URL || undefined;
  const { server, baseURL } = await startAccountProxy({
    route: process.env.CLIPROXY_CLAUDE_ROUTE,
    prefix: process.env.CLIPROXY_ACCOUNT_PREFIX,
    upstream: process.env.CLIPROXY_BASE_URL,
    fallbackUpstream,
    failover: fallbackUpstream ? {
      settings: connectTimeoutSetting(process.env.CLIPROXY_CONNECT_TIMEOUT_MS),
      log: transitionLog(process.env.CLIPROXY_FAILOVER_LOG || defaultFailoverLog),
    } : {},
  });
  const child = spawn(binary, process.argv.slice(2), {
    stdio: "inherit",
    env: { ...process.env, ANTHROPIC_BASE_URL: baseURL, ANTHROPIC_AUTH_TOKEN: "", CLAUDE_CODE_OAUTH_TOKEN: "" },
  });

  let state = "running";
  let escalation;
  let proxyClosed = false;
  const close = () => {
    if (proxyClosed) return;
    proxyClosed = true;
    server.closeAllConnections();
    server.close();
  };
  const finish = () => {
    state = "exited";
    clearTimeout(escalation);
    close();
  };
  const stop = (signal) => {
    // Repeated signals must not restart the shutdown grace period.
    if (state !== "running") return;
    state = "stopping";
    close();
    child.kill(signal);
    escalation = setTimeout(() => child.kill("SIGKILL"), childShutdownGraceMs);
  };

  child.once("error", () => {
    console.error("Unable to launch Claude Code.");
    finish();
    process.exitCode = 1;
  });
  child.once("exit", (code, signal) => {
    finish();
    process.exitCode = code ?? (signal ? 128 + os.constants.signals[signal] : 0);
  });
  for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) process.on(signal, () => stop(signal));
}

if (launcherPath && fs.existsSync(launcherPath)
    && fs.realpathSync(launcherSource) === fs.realpathSync(launcherPath)) {
  main().catch((error) => { console.error(error.message); process.exitCode = 1; });
}
