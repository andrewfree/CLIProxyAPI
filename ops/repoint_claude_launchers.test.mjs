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
  instances.unrelated = { driver: "claudeAgent", config: { binaryPath: "/other/launcher" }, environment: [
    { name: "OTHER_API_KEY", value: "fixture-private", sensitive: true },
    { name: "ANTHROPIC_API_KEY", value: "fixture-unflagged" },
  ] };
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

function backupRoot(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "repoint-test-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

test("repoints precisely five Claude paths, writes private redacted snapshots, and rolls back", async (t) => {
  const { client, saved, writes } = fixture();
  const before = structuredClone(saved);
  const directory = backupRoot(t);
  assert.deepEqual(await repoint(client, "report"), Array(5).fill(oldPath));
  assert.equal(writes.length, 0);
  assert.deepEqual(fs.readdirSync(directory), []);
  const apply = path.join(directory, "apply");
  assert.deepEqual(await repoint(client, "apply", apply), Array(5).fill(newPath));
  for (const [file, target] of [["settings-before.json", oldPath], ["settings-after.json", newPath]]) {
    const snapshot = path.join(apply, file);
    const data = fs.readFileSync(snapshot, "utf8");
    assert.equal(fs.statSync(snapshot).mode & 0o777, 0o600);
    assert.doesNotMatch(data, /fixture-private|fixture-unflagged/);
    const providers = JSON.parse(data).providerInstances;
    assert.equal(providers.named_one.config.binaryPath, target);
    assert.equal(providers.named_one.environment[1].valueRedacted, true);
    assert.deepEqual(providers.unrelated.environment.map(({ value }) => value), ["<redacted>", "<redacted>"]);
  }
  for (const id of ids) before[id].config.binaryPath = newPath;
  assert.deepEqual(saved, before);
  const repeat = path.join(directory, "repeat");
  assert.deepEqual(await repoint(client, "apply", repeat), Array(5).fill(newPath));
  assert.equal(writes.length, 1);
  assert.equal(fs.existsSync(repeat), false);
  assert.deepEqual(await repoint(client, "rollback", path.join(directory, "rollback")), Array(5).fill(oldPath));
  assert.equal(writes.length, 2);
});

test("refuses to patch a changed profile inventory or incomplete secret state", async (t) => {
  const { client, saved, writes } = fixture();
  const directory = backupRoot(t);
  saved.named_one.environment.pop();
  await assert.rejects(repoint(client, "apply", directory), /redacted T3 secret/);
  delete saved.named_one;
  await assert.rejects(repoint(client, "apply", directory), /five Mac/);
  assert.equal(writes.length, 0);
  assert.deepEqual(fs.readdirSync(directory), []);
});

for (const file of ["settings-before.json", "settings-after.json"]) {
  test(`refuses to overwrite ${file} before updating settings`, async (t) => {
    const { client, writes } = fixture();
    const directory = backupRoot(t);
    const target = path.join(directory, file);
    fs.writeFileSync(target, "keep this backup");
    await assert.rejects(repoint(client, "apply", directory), new RegExp(file));
    assert.equal(writes.length, 0);
    assert.equal(fs.readFileSync(target, "utf8"), "keep this backup");
  });
}

test("the repoint CLI reports its argument errors through a symlink", (t) => {
  const alias = path.join(backupRoot(t), "repoint.mjs");
  fs.symlinkSync(fileURLToPath(new URL("./repoint_claude_launchers.mjs", import.meta.url)), alias);
  const result = spawnSync(process.execPath, [alias, "--base-dir", "/unused", "--apply"], {
    env: {}, encoding: "utf8", timeout: 5000,
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /--backup-dir is required/);
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
