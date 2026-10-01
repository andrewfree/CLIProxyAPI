#!/usr/bin/env node
import http from "node:http";
import https from "node:https";
import os from "node:os";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { pathToFileURL } from "node:url";

const hopHeaders = new Set([
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
]);

const supportedOperations = new Set([
  "POST /v1/messages", "POST /v1/messages/count_tokens", "GET /v1/models",
]);

const childShutdownGraceMs = 5000;

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

export async function startAccountProxy({ prefix, upstream }) {
  if (!/^[a-zA-Z0-9_-]+$/.test(prefix ?? "")) throw new Error("A valid CLIPROXY_ACCOUNT_PREFIX is required.");
  const target = new URL(upstream);
  if (!["http:", "https:"].includes(target.protocol) || target.username || target.password || target.search || target.hash) {
    throw new Error("CLIPROXY_BASE_URL must be an HTTP origin without credentials or query parameters.");
  }
  const transport = target.protocol === "https:" ? https : http;
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
          return fail(response, 415, "An account-pinned request requires an application/json body.");
        }
        const value = JSON.parse(await requestBody(request));
        if (value === null || typeof value !== "object" || Array.isArray(value) || typeof value.model !== "string" || !value.model) {
          return fail(response, 400, "A model is required for an account-pinned request.");
        }
        if (value.model.includes("/") && !value.model.startsWith(`${prefix}/`)) {
          return fail(response, 400, "This Claude entry is pinned to a different account.");
        }
        if (!value.model.startsWith(`${prefix}/`)) value.model = `${prefix}/${value.model}`;
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
    const upstreamRequest = transport.request({
      protocol: target.protocol, hostname: target.hostname, port: target.port,
      method: request.method, path: `${target.pathname.replace(/\/$/, "")}${request.url}`, headers,
    }, async (upstreamResponse) => {
      const outgoing = forwardedHeaders(upstreamResponse.headers);
      if (request.method === "GET" && path === "/v1/models" && upstreamResponse.statusCode === 200) {
        try {
          const chunks = [];
          for await (const chunk of upstreamResponse) chunks.push(chunk);
          const value = JSON.parse(Buffer.concat(chunks));
          value.data = value.data.filter((model) => model.id.startsWith(`${prefix}/`)).map((model) => ({
            ...model, id: model.id.slice(prefix.length + 1),
          }));
          response.writeHead(200, outgoing);
          response.end(JSON.stringify(value));
        } catch {
          fail(response, 502, "The proxy returned an invalid model list.");
        }
        return;
      }
      response.writeHead(upstreamResponse.statusCode, outgoing);
      upstreamResponse.on("error", () => response.destroy());
      upstreamResponse.pipe(response);
    });
    upstreamRequest.on("error", () => fail(response, 502, "CLIProxy is unavailable for the selected account."));
    response.on("close", () => upstreamRequest.destroy());
    upstreamRequest.end(body);
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return { server, baseURL: `http://127.0.0.1:${server.address().port}` };
}

async function main() {
  const binary = process.env.CLIPROXY_CLAUDE_BINARY;
  if (!binary) throw new Error("CLIPROXY_CLAUDE_BINARY is required.");
  const { server, baseURL } = await startAccountProxy({
    prefix: process.env.CLIPROXY_ACCOUNT_PREFIX,
    upstream: process.env.CLIPROXY_BASE_URL,
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

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => { console.error(error.message); process.exitCode = 1; });
}
