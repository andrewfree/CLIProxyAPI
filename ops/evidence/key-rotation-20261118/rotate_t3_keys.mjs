#!/usr/bin/env node
// Replace ANTHROPIC_API_KEY for named T3 provider instances through server.updateSettings.
// Usage: node rotate_t3_keys.mjs <t3-base-dir> <configure_claude_pool.mjs> <backup-dir> <key-file> <keyIndex> <id> [id...]
// key-file: JSON {"new": [...]}; keyIndex selects which new key. Prints no secret values.
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [baseDir, poolScript, backupDir, keyFile, keyIndex, ...ids] = process.argv.slice(2);
if (!ids.length) throw new Error("usage: base-dir pool-script backup-dir key-file key-index id...");
const { connectT3 } = await import(pathToFileURL(poolScript).href);
const newKey = JSON.parse(fs.readFileSync(keyFile, "utf8")).new[Number(keyIndex)];
if (!newKey) throw new Error("no such new key");
fs.mkdirSync(backupDir, { recursive: true, mode: 0o700 });
const client = await connectT3(baseDir);
try {
  const { settings } = await client.rpc("server.getConfig");
  const providers = structuredClone(settings.providerInstances);
  for (const id of ids) {
    const p = providers[id];
    if (!p || p.driver !== "claudeAgent") throw new Error(`refusing: ${id} is not a claudeAgent instance`);
    const env = p.environment ?? [];
    const idx = env.findIndex((v) => v.name === "ANTHROPIC_API_KEY");
    if (idx < 0 || !env[idx].sensitive) throw new Error(`refusing: ${id} has no sensitive ANTHROPIC_API_KEY`);
    env[idx] = { name: "ANTHROPIC_API_KEY", value: newKey, sensitive: true };
  }
  const stamp = new Date().toISOString().replace(/[:.]/g, "");
  fs.writeFileSync(path.join(backupDir, `settings-before-${stamp}.json`), JSON.stringify(settings, null, 2), { flag: "wx", mode: 0o600 });
  await client.rpc("server.updateSettings", { patch: { providerInstances: providers } });
  const { settings: after } = await client.rpc("server.getConfig");
  const report = {};
  for (const [id, p] of Object.entries(after.providerInstances)) {
    const s = (p.environment ?? []).find((v) => v.name === "ANTHROPIC_API_KEY");
    if (s) report[id] = { sensitive: !!s.sensitive, redacted: !!s.valueRedacted, valueVisible: !!s.value };
  }
  fs.writeFileSync(path.join(backupDir, `settings-after-${stamp}.json`), JSON.stringify(after, null, 2), { flag: "wx", mode: 0o600 });
  console.log(JSON.stringify({ updated: ids, apiKeyEntries: report }));
} finally {
  client.close();
}
