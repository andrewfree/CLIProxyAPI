#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { isDeepStrictEqual, parseArgs } from "node:util";
import { fileURLToPath } from "node:url";
import { connectT3, withoutSecrets } from "./configure_claude_pool.mjs";

const checkout = "/Users/rever/cliproxy/ops/claude_account_launcher.mjs";
const bundle = "/Users/rever/cliproxy-live/ops/claude_account_launcher.mjs";

class LauncherError extends Error {}

function writeBackup(directory, name, settings) {
  try {
    fs.writeFileSync(path.join(directory, name), JSON.stringify(withoutSecrets(settings), null, 2), {
      flag: "wx", mode: 0o600,
    });
  } catch {
    throw new LauncherError(`Could not write ${name} in the private backup directory`);
  }
}

export async function repoint(client, mode, backupDir) {
  const read = async () => (await client.rpc("server.getConfig")).settings;
  const beforeSettings = await read();
  const before = beforeSettings.providerInstances;
  const ids = Object.keys(before).filter((id) => [checkout, bundle].includes(before[id]?.config?.binaryPath));
  const paths = (providers) => ids.map((id) => providers[id]?.config?.binaryPath);
  if (ids.length !== 5 || !["claude_cliproxy", "claude_cliproxy_mac"].every((id) => ids.includes(id))
      || ids.some((id) => before[id].driver !== "claudeAgent")) {
    throw new LauncherError("Expected the five Mac CLIProxy Claude profiles");
  }
  if (mode === "report") return paths(before);
  if (!["apply", "rollback"].includes(mode)) throw new LauncherError("Invalid mode");
  if (!backupDir) throw new LauncherError("--backup-dir is required for apply and rollback");

  for (const id of ids) {
    const keys = before[id].environment?.filter(({ name }) => name === "ANTHROPIC_API_KEY");
    if (keys?.length !== 1 || !keys[0].sensitive || !keys[0].valueRedacted || keys[0].value) {
      throw new LauncherError("Expected a redacted T3 secret for every selected launcher");
    }
  }
  const target = mode === "rollback" ? checkout : bundle;
  if (paths(before).every((value) => value === target)) return paths(before);
  const expected = { ...before };
  for (const id of ids) {
    const profile = before[id];
    expected[id] = { ...profile, config: { ...profile.config, binaryPath: target } };
  }
  try {
    fs.mkdirSync(backupDir, { recursive: true, mode: 0o700 });
    if (fs.lstatSync(path.join(backupDir, "settings-after.json"), { throwIfNoEntry: false })) {
      throw new LauncherError("settings-after.json already exists. Use a new private backup directory");
    }
  } catch (error) {
    if (error instanceof LauncherError) throw error;
    throw new LauncherError("Could not prepare the private backup directory");
  }
  writeBackup(backupDir, "settings-before.json", beforeSettings);
  await client.rpc("server.updateSettings", { patch: { providerInstances: expected } });
  const afterSettings = await read();
  writeBackup(backupDir, "settings-after.json", afterSettings);
  const after = afterSettings.providerInstances;
  if (!isDeepStrictEqual(after, expected)) throw new LauncherError("T3 saved settings did not match the path patch");
  return paths(after);
}

async function main() {
  let values;
  try {
    ({ values } = parseArgs({
      options: {
        "base-dir": { type: "string" },
        "backup-dir": { type: "string" },
        report: { type: "boolean" },
        apply: { type: "boolean" },
        rollback: { type: "boolean" },
      },
    }));
  } catch {
    throw new LauncherError("Invalid launcher arguments. Use --base-dir and one of --report, --apply, or --rollback");
  }
  const modes = ["report", "apply", "rollback"].filter((mode) => values[mode]);
  if (!values["base-dir"] || modes.length !== 1) throw new LauncherError("Choose one launcher action and a T3 base directory");
  if (!values.report && !values["backup-dir"]) throw new LauncherError("--backup-dir is required for apply and rollback");
  const scopes = modes[0] === "report" ? ["orchestration:read"] : undefined;
  const client = await connectT3(values["base-dir"], scopes);
  try {
    console.log(JSON.stringify(await repoint(client, modes[0], values["backup-dir"])));
  } finally {
    client.close();
  }
}

const entry = process.argv[1];
const isMain = import.meta.main || Boolean(entry && fs.existsSync(entry)
  && fs.realpathSync(fileURLToPath(import.meta.url)) === fs.realpathSync(entry));
if (isMain) {
  main().catch((error) => {
    console.error(error instanceof LauncherError ? error.message
      : "T3 launcher operation failed. Use --report to inspect saved paths.");
    process.exitCode = 1;
  });
}
