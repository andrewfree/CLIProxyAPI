#!/usr/bin/env node
// Read-only: list Claude CLIProxy profiles with non-secret routing fields only.
import { connectT3 } from "../../configure_claude_pool.mjs";
const client = await connectT3(process.argv[2], ["orchestration:read"]);
const mask = (v) => String(v).replace(/[a-z0-9-]+\.[a-z0-9-]+\.ts\.net/gi, "will.<tailnet>.ts.net");
try {
  const { settings } = await client.rpc("server.getConfig");
  for (const [id, p] of Object.entries(settings.providerInstances ?? {})) {
    if (!String(p?.config?.binaryPath ?? "").includes("claude_account_launcher")) continue;
    const env = Object.fromEntries((p.environment ?? []).map((e) => [e.name ?? e.key, e]));
    const pick = (k) => (env[k] && !env[k].sensitive ? mask(env[k].value ?? "") : env[k] ? "<sensitive>" : null);
    console.log(JSON.stringify({ id, name: p.displayName ?? p.name ?? null, enabled: p.enabled ?? null, binaryPath: p.config.binaryPath,
      route: pick("CLIPROXY_CLAUDE_ROUTE"), prefix: pick("CLIPROXY_CLAUDE_PREFIX"), base: pick("CLIPROXY_BASE_URL"), fallback: pick("CLIPROXY_FALLBACK_URL") }));
  }
} finally {
  client.close();
}
