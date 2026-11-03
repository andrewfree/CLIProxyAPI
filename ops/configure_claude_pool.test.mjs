import test from "node:test";
import assert from "node:assert/strict";
import { poolProviders } from "./configure_claude_pool.mjs";

const options = {
  id: "claude_cliproxy", label: "Claude - CLIProxy pool (Will primary)",
  upstream: "http://127.0.0.1:8317", launcher: "/repo/ops/claude_account_launcher.mjs",
  binary: "/bin/claude", apiKey: "fixture-key",
};
const existing = {
  claudeAgent: { driver: "claudeAgent", enabled: true, config: { binaryPath: "/bin/claude", homePath: "/existing/home" } },
  named: { driver: "claudeAgent", environment: [{ name: "CLIPROXY_ACCOUNT_PREFIX", value: "t3-pro" }] },
  codex: { driver: "codex", enabled: true },
};

test("adds a pool without changing pinned, direct, or unrelated configurations", () => {
  const before = structuredClone(existing);
  const result = poolProviders(existing, options);
  assert.deepEqual(existing, before);
  assert.deepEqual(result.named, existing.named);
  assert.deepEqual(result.codex, existing.codex);
  assert.deepEqual(result.claudeAgent.config, existing.claudeAgent.config);
  assert.equal(result.claudeAgent.displayName, "Claude - Direct login");
  const pool = result.claude_cliproxy;
  assert.equal(pool.config.homePath, "/existing/home");
  const env = Object.fromEntries(pool.environment.map((variable) => [variable.name, variable.value]));
  assert.equal(env.CLIPROXY_CLAUDE_ROUTE, "shared-pool");
  assert.equal(env.CLIPROXY_ACCOUNT_PREFIX, "");
  assert.equal(env.CLIPROXY_CLAUDE_BINARY, "/bin/claude");
  assert.equal(env.DO_NOT_TRACK, "1");
  assert.equal(env.ANTHROPIC_AUTH_TOKEN, "");
  assert.equal(env.CLAUDE_CODE_OAUTH_TOKEN, "");
  assert.equal(pool.environment.find((v) => v.name === "ANTHROPIC_API_KEY").sensitive, true);
});

test("reapplying configuration is idempotent and preserves a custom direct label", () => {
  const first = poolProviders(existing, options);
  first.claudeAgent.displayName = "My direct subscription";
  const second = poolProviders(first, options);
  assert.deepEqual(second, first);
});

test("refuses to overwrite another provider or accept an ambiguous endpoint", () => {
  for (const id of ["claudeAgent", "named", "codex"]) assert.throws(() => poolProviders(existing, { ...options, id }), /Refusing to replace/);
  for (const upstream of ["http://user:secret@localhost", "http://localhost/v1", "http://localhost?key=secret", "file:///host"]) {
    assert.throws(() => poolProviders(existing, { ...options, upstream }), /HTTP origin/);
  }
});
