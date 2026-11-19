#!/usr/bin/env node
// Enable or roll back session-start Codex failover on codex_cliproxy through server.updateSettings.
// Usage: node codex_failover_profile.mjs <t3-base-dir> <backup-dir> apply|rollback
// Prints only a verification report; backups are redacted (withoutSecrets) and mode 0600.
import fs from "node:fs";
import path from "node:path";
import { connectT3, withoutSecrets } from "../../configure_claude_pool.mjs";
const [baseDir, backupDir, mode] = process.argv.slice(2);
if (!["apply", "rollback"].includes(mode)) throw new Error("usage: base-dir backup-dir apply|rollback");
const id = "codex_cliproxy";
const wrapper = "/Users/rever/cliproxy-live/ops/codex_fallback_launcher.mjs";
const plain = "/opt/homebrew/bin/codex";
const env = [
  { name: "CLIPROXY_FALLBACK_URL", value: "http://127.0.0.1:8317", sensitive: false },
  { name: "CODEX_REAL_BINARY", value: plain, sensitive: false },
];
fs.mkdirSync(backupDir, { recursive: true, mode: 0o700 });
const client = await connectT3(baseDir);
try {
  const { settings } = await client.rpc("server.getConfig");
  const before = settings.providerInstances;
  const profile = before[id];
  if (profile?.driver !== "codex") throw new Error(`refusing: ${id} is not a codex profile`);
  const expectFrom = mode === "apply" ? plain : wrapper;
  if (profile.config?.binaryPath !== expectFrom) throw new Error(`refusing: ${id} binaryPath is not the expected starting value`);
  if ((profile.environment ?? []).some((v) => v.sensitive)) throw new Error(`refusing: ${id} has sensitive environment values`);
  const next = { ...profile, config: { ...profile.config, binaryPath: mode === "apply" ? wrapper : plain },
                 environment: mode === "apply" ? env : [] };
  const stamp = new Date().toISOString().replace(/[:.]/g, "");
  const backup = (name, value) => fs.writeFileSync(path.join(backupDir, `settings-${mode}-${name}-${stamp}.json`),
    JSON.stringify(withoutSecrets(value), null, 2), { flag: "wx", mode: 0o600 });
  backup("before", settings);
  await client.rpc("server.updateSettings", { patch: { providerInstances: { ...before, [id]: next } } });
  const { settings: after } = await client.rpc("server.getConfig");
  backup("after", after);
  const saved = after.providerInstances[id];
  // T3 may return keys in a different order, so compare with sorted keys.
  const canon = (v) => Array.isArray(v) ? v.map(canon) : v && typeof v === "object"
    ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, canon(v[k])])) : v;
  const same = (a, b) => JSON.stringify(canon(a)) === JSON.stringify(canon(b));
  const others = Object.keys(before).filter((k) => k !== id);
  const report = {
    profile: id, mode,
    binaryPath: saved.config.binaryPath,
    environment: (saved.environment ?? []).map((v) => [v.name, v.sensitive ? "<sensitive>" : v.value]),
    launchArgsUnchanged: saved.config.launchArgs === profile.config.launchArgs,
    restUnchanged: same({ ...saved, config: { ...saved.config, binaryPath: null }, environment: null },
                        { ...profile, config: { ...profile.config, binaryPath: null }, environment: null }),
    otherProfilesUnchanged: others.every((k) => same(after.providerInstances[k], before[k])),
    profileCount: [others.length + 1, Object.keys(after.providerInstances).length],
  };
  console.log(JSON.stringify(report));
  if (!(report.launchArgsUnchanged && report.restUnchanged && report.otherProfilesUnchanged &&
        report.binaryPath === next.config.binaryPath && same(report.environment, next.environment.map((v) => [v.name, v.value])))) {
    process.exitCode = 1;
    console.error("verification failed; compare with the private before backup");
  }
} finally {
  client.close();
}
