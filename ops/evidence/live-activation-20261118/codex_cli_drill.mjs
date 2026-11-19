#!/usr/bin/env node
// Read codex_cliproxy's launchArgs and hand them to the drill without printing them.
import { execFileSync } from "node:child_process";
import { connectT3 } from "../../configure_claude_pool.mjs";
const [baseDir, drillDir] = process.argv.slice(2);
const client = await connectT3(baseDir, ["orchestration:read"]);
let args;
try {
  ({ settings: { providerInstances: { codex_cliproxy: { config: { launchArgs: args } } } } } = await client.rpc("server.getConfig"));
} finally {
  client.close();
}
// The arguments go on stdin, so an error message from execFileSync cannot echo them.
try {
  process.stdout.write(execFileSync("python3", [new URL("./codex_cli_drill.py", import.meta.url).pathname, drillDir],
    { input: args, encoding: "utf8" }));
} catch (error) {
  process.stdout.write(error.stdout ?? "");
  console.error(`drill failed with exit ${error.status ?? "unknown"}`);
  process.exitCode = 1;
}
