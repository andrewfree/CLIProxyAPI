import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { repoint } from "./repoint_claude_launchers.mjs";

const oldPath = "/Users/rever/cliproxy/ops/claude_account_launcher.mjs";
const newPath = "/Users/rever/cliproxy-live/ops/claude_account_launcher.mjs";
const ids = ["claude_cliproxy", "claude_cliproxy_mac", "named_one", "named_two", "named_three"];

function fixture() {
  const instances = Object.fromEntries(ids.map((id) => [id, {
    driver: "claudeAgent",
    config: { binaryPath: oldPath, launchArgs: "fixture args" },
    environment: [
      { name: "UNCHANGED_FIXTURE", value: "existing value" },
      { name: "ANTHROPIC_API_KEY", value: "", sensitive: true, valueRedacted: true },
    ],
  }]));
  instances.unrelated = { driver: "claudeAgent", config: { binaryPath: "/other/launcher" } };
  const saved = structuredClone(instances);
  const writes = [];
  return {
    saved,
    writes,
    client: {
      async rpc(tag, request) {
        if (tag === "server.getConfig") return { settings: { providerInstances: structuredClone(saved) } };
        assert.equal(tag, "server.updateSettings");
        assert.deepEqual(Object.keys(request.patch), ["providerInstances"]);
        writes.push(request.patch);
        Object.assign(saved, structuredClone(request.patch.providerInstances));
      },
    },
  };
}

test("repoints precisely five Claude paths, reads back the saved map, and rolls back", async () => {
  const { client, saved, writes } = fixture();
  const before = structuredClone(saved);
  assert.deepEqual(await repoint(client, "report"), Array(5).fill(oldPath));
  assert.equal(writes.length, 0);
  assert.deepEqual(await repoint(client, "apply"), Array(5).fill(newPath));
  for (const id of ids) before[id].config.binaryPath = newPath;
  assert.deepEqual(saved, before);
  assert.deepEqual(await repoint(client, "apply"), Array(5).fill(newPath));
  assert.equal(writes.length, 1);
  assert.deepEqual(await repoint(client, "rollback"), Array(5).fill(oldPath));
  assert.equal(writes.length, 2);
});

test("refuses to patch a changed profile inventory or incomplete secret state", async () => {
  const { client, saved, writes } = fixture();
  saved.named_one.environment.pop();
  await assert.rejects(repoint(client, "apply"), /redacted T3 secret/);
  delete saved.named_one;
  await assert.rejects(repoint(client, "apply"), /five Mac/);
  assert.equal(writes.length, 0);
});

test("the launcher executes through a published file alias", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "claude-launcher-bundle-test-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const launcher = path.join(directory, "claude_account_launcher.mjs");
  fs.symlinkSync(fileURLToPath(new URL("./claude_account_launcher.mjs", import.meta.url)), launcher);
  const result = spawnSync(process.execPath, [launcher, "fixture-launched"], {
    env: { CLIPROXY_CLAUDE_BINARY: "/bin/echo", CLIPROXY_CLAUDE_ROUTE: "shared-pool",
      CLIPROXY_BASE_URL: "http://127.0.0.1:1" },
    encoding: "utf8", timeout: 5000,
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), "fixture-launched");
});
