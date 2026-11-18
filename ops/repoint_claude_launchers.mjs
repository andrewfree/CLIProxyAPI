#!/usr/bin/env node
import { isDeepStrictEqual, parseArgs } from "node:util";
import { pathToFileURL } from "node:url";
import { connectT3 } from "./configure_claude_pool.mjs";

const checkout = "/Users/rever/cliproxy/ops/claude_account_launcher.mjs";
const bundle = "/Users/rever/cliproxy-live/ops/claude_account_launcher.mjs";

export async function repoint(client, mode) {
  const read = async () => (await client.rpc("server.getConfig")).settings.providerInstances;
  const before = await read();
  const ids = Object.keys(before).filter((id) => [checkout, bundle].includes(before[id]?.config?.binaryPath));
  const paths = (providers) => ids.map((id) => providers[id]?.config?.binaryPath);
  if (ids.length !== 5 || !["claude_cliproxy", "claude_cliproxy_mac"].every((id) => ids.includes(id))
      || ids.some((id) => before[id].driver !== "claudeAgent")) {
    throw new Error("Expected the five Mac CLIProxy Claude profiles");
  }
  if (mode === "report") return paths(before);
  if (!["apply", "rollback"].includes(mode)) throw new Error("Invalid mode");

  for (const id of ids) {
    const keys = before[id].environment?.filter(({ name }) => name === "ANTHROPIC_API_KEY");
    if (keys?.length !== 1 || !keys[0].sensitive || !keys[0].valueRedacted || keys[0].value) {
      throw new Error("Expected a redacted T3 secret for every selected launcher");
    }
  }
  const target = mode === "rollback" ? checkout : bundle;
  if (paths(before).every((value) => value === target)) return paths(before);
  const expected = { ...before };
  for (const id of ids) {
    const profile = before[id];
    expected[id] = { ...profile, config: { ...profile.config, binaryPath: target } };
  }
  await client.rpc("server.updateSettings", { patch: { providerInstances: expected } });
  const after = await read();
  if (!isDeepStrictEqual(after, expected)) throw new Error("T3 saved settings did not match the path patch");
  return paths(after);
}

async function main() {
  const { values } = parseArgs({
    options: {
      "base-dir": { type: "string" },
      report: { type: "boolean" },
      apply: { type: "boolean" },
      rollback: { type: "boolean" },
    },
  });
  const modes = ["report", "apply", "rollback"].filter((mode) => values[mode]);
  if (!values["base-dir"] || modes.length !== 1) throw new Error("Choose one launcher action and a T3 base directory");
  const scopes = modes[0] === "report" ? ["orchestration:read"] : undefined;
  const client = await connectT3(values["base-dir"], scopes);
  try {
    console.log(JSON.stringify(await repoint(client, modes[0])));
  } finally {
    client.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => {
    console.error("T3 launcher operation did not verify. Use --report to inspect saved paths.");
    process.exitCode = 1;
  });
}
