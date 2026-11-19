#!/usr/bin/env node
// Read-only: print codex_cliproxy's launchArgs with tailnet names masked.
import { connectT3 } from "../../configure_claude_pool.mjs";
const client = await connectT3(process.argv[2], ["orchestration:read"]);
try {
  const { settings } = await client.rpc("server.getConfig");
  const p = settings.providerInstances.codex_cliproxy;
  const args = p.config?.launchArgs;
  console.log(JSON.stringify({ type: Array.isArray(args) ? "array" : typeof args,
    launchArgs: JSON.stringify(args).replace(/[a-z0-9-]+\.[a-z0-9-]+\.ts\.net/gi, "will.<tailnet>.ts.net"),
    configKeys: Object.keys(p.config ?? {}), topKeys: Object.keys(p) }));
} finally {
  client.close();
}
