#!/usr/bin/env node
// Read-only: print the codex_cliproxy profile's driver, binaryPath and environment variable names.
import { connectT3 } from "../../configure_claude_pool.mjs";
const client = await connectT3(process.argv[2], ["orchestration:read"]);
try {
  const { settings } = await client.rpc("server.getConfig");
  const p = settings.providerInstances?.codex_cliproxy;
  console.log(JSON.stringify({ driver: p?.driver, binaryPath: p?.config?.binaryPath ?? null,
    environmentNames: (p?.environment ?? []).map((e) => e.name ?? e.key ?? "?") }));
} finally {
  client.close();
}
