#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { parseArgs } from "node:util";
import { fileURLToPath, pathToFileURL } from "node:url";

export function poolProviders(providers, { id, label, upstream, launcher, binary, apiKey }) {
  if (!/^[a-zA-Z0-9_-]+$/.test(id)) throw new Error("A valid profile ID is required.");
  const origin = new URL(upstream);
  if (!["http:", "https:"].includes(origin.protocol) || origin.username || origin.password || origin.search || origin.hash || origin.pathname !== "/") {
    throw new Error("The upstream must be an HTTP origin without credentials or a path.");
  }
  if (!path.isAbsolute(launcher) || !path.isAbsolute(binary) || !apiKey?.trim() || !label?.trim()) {
    throw new Error("Absolute launcher and Claude paths, a label, and an API key are required.");
  }
  const previous = providers[id];
  if (previous && (previous.driver !== "claudeAgent" || !previous.environment?.some((v) => v.name === "CLIPROXY_CLAUDE_ROUTE" && v.value === "shared-pool"))) {
    throw new Error("Refusing to replace an existing provider that is not a shared Claude pool.");
  }
  const direct = providers.claudeAgent;
  if (direct?.driver !== "claudeAgent") throw new Error("The native Claude provider must exist.");
  return {
    ...providers,
    claudeAgent: { ...direct, displayName: direct.displayName || "Claude - Direct login" },
    [id]: {
      driver: "claudeAgent", displayName: label, enabled: true,
      config: { ...direct.config, binaryPath: launcher },
      environment: [
        { name: "CLIPROXY_CLAUDE_ROUTE", value: "shared-pool", sensitive: false },
        { name: "CLIPROXY_ACCOUNT_PREFIX", value: "", sensitive: false },
        { name: "CLIPROXY_BASE_URL", value: origin.origin, sensitive: false },
        { name: "CLIPROXY_CLAUDE_BINARY", value: binary, sensitive: false },
        { name: "ANTHROPIC_API_KEY", value: apiKey.trim(), sensitive: true },
        { name: "ANTHROPIC_AUTH_TOKEN", value: "", sensitive: false },
        { name: "CLAUDE_CODE_OAUTH_TOKEN", value: "", sensitive: false },
        { name: "DO_NOT_TRACK", value: "1", sensitive: false },
      ],
    },
  };
}

export async function connectT3(baseDir, scopes = ["orchestration:read", "settings:write", "providers:manage"]) {
  const runtime = JSON.parse(fs.readFileSync(path.join(baseDir, "userdata/server-runtime.json"), "utf8"));
  const versionDir = path.join(baseDir, "runtime/versions", runtime.serverVersion);
  const t3 = path.join(versionDir, "t3");
  const cli = (args) => execFileSync(t3, args, { encoding: "utf8", env: { ...process.env, DO_NOT_TRACK: "1" }, stdio: ["ignore", "pipe", "pipe"] });
  const auth = JSON.parse(cli(["auth", "session", "issue", "--base-dir", baseDir, "--ttl", "5m", "--label", "CLIProxy Claude pool configuration", "--json", ...scopes.flatMap((scope) => ["--scope", scope])]));
  let socket;
  const pending = new Map();
  let counter = 0;
  const rejectPending = () => {
    for (const item of pending.values()) item.reject(new Error("T3 settings connection closed."));
    pending.clear();
  };
  const close = () => {
    socket?.close();
    rejectPending();
    cli(["auth", "session", "revoke", "--base-dir", baseDir, auth.sessionId]);
  };
  try {
    const { WebSocket } = createRequire(path.join(versionDir, "t3"))("undici");
    socket = new WebSocket(`ws://127.0.0.1:${runtime.port}/ws?orchestrationProtocol=2`, { headers: { Authorization: `Bearer ${auth.token}` } });
    socket.addEventListener("message", ({ data }) => {
      const parsed = JSON.parse(String(data));
      for (const value of Array.isArray(parsed) ? parsed : [parsed]) {
        if (value._tag === "Ping") { socket.send(JSON.stringify({ _tag: "Pong" })); continue; }
        if (value._tag !== "Exit") continue;
        const item = pending.get(String(value.requestId));
        if (!item) continue;
        pending.delete(String(value.requestId));
        if (value.exit._tag === "Success") item.resolve(value.exit.value);
        else {
          const tags = [...JSON.stringify(value.exit).matchAll(/"_tag":"([A-Za-z0-9]+)"/g)].map((match) => match[1]);
          item.reject(new Error(`T3 RPC failed: ${item.tag} (${tags.join(", ")})`));
        }
      }
    });
    socket.addEventListener("close", rejectPending);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("T3 settings connection timed out.")), 30000);
      socket.addEventListener("open", () => { clearTimeout(timer); resolve(); }, { once: true });
      socket.addEventListener("error", () => { clearTimeout(timer); reject(new Error("Cannot connect to the running T3 server.")); }, { once: true });
    });
    return {
      runtime, close,
      rpc(tag, payload = {}) {
        const id = String(++counter);
        return new Promise((resolve, reject) => {
          const timer = setTimeout(() => { pending.delete(id); reject(new Error(`T3 RPC timed out: ${tag}`)); }, 30000);
          pending.set(id, { tag, resolve: (value) => { clearTimeout(timer); resolve(value); }, reject: (error) => { clearTimeout(timer); reject(error); } });
          socket.send(JSON.stringify({ _tag: "Request", id, tag, payload, headers: [] }));
        });
      },
    };
  } catch (error) {
    close();
    throw error;
  }
}

async function main() {
  const { values } = parseArgs({ options: Object.fromEntries(["base-dir", "profile-id", "label", "upstream", "claude-binary", "api-key-file", "backup-dir"].map((name) => [name, { type: "string" }])) });
  for (const name of ["base-dir", "profile-id", "label", "upstream", "claude-binary", "api-key-file", "backup-dir"]) {
    if (!values[name]) throw new Error(`--${name} is required.`);
  }
  if (fs.statSync(values["api-key-file"]).mode & 0o077) throw new Error("The API key file must be private (mode 0600).");
  fs.mkdirSync(values["backup-dir"], { recursive: true, mode: 0o700 });
  const apiKey = fs.readFileSync(values["api-key-file"], "utf8");
  const client = await connectT3(values["base-dir"]);
  try {
    const { settings } = await client.rpc("server.getConfig");
    const providers = poolProviders(settings.providerInstances, {
      id: values["profile-id"], label: values.label, upstream: values.upstream,
      launcher: fileURLToPath(new URL("./claude_account_launcher.mjs", import.meta.url)), binary: values["claude-binary"], apiKey,
    });
    fs.writeFileSync(path.join(values["backup-dir"], "settings-before.json"), JSON.stringify(settings, null, 2), { flag: "wx", mode: 0o600 });
    await client.rpc("server.updateSettings", { patch: { providerInstances: providers } });
    const { settings: after } = await client.rpc("server.getConfig");
    const saved = after.providerInstances[values["profile-id"]];
    const secret = saved.environment.find((variable) => variable.name === "ANTHROPIC_API_KEY");
    if (!secret?.sensitive || !secret.valueRedacted || secret.value) throw new Error("API key secret storage was not confirmed.");
    fs.writeFileSync(path.join(values["backup-dir"], "settings-after.json"), JSON.stringify(after, null, 2), { flag: "wx", mode: 0o600 });
    console.log(JSON.stringify({ profile: values["profile-id"], label: saved.displayName, upstream: values.upstream, secretStoredAndRedacted: true, serverVersion: client.runtime.serverVersion }));
  } finally {
    client.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { console.error("Claude pool configuration failed. Check the arguments and private backup directory; no secret values are printed."); process.exitCode = 1; });
}
