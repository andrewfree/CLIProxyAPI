import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defaultPoolLauncher, poolProviders } from "./configure_claude_pool.mjs";

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

test("a new Mac pool defaults to the fleet bundle and a rerun preserves the installed launcher", () => {
  const hosts = JSON.parse(fs.readFileSync(new URL("./fleet/hosts.json", import.meta.url), "utf8"));
  const launcher = defaultPoolLauncher("darwin");
  assert.equal(launcher, path.join(hosts.m5.bundle_root, "claude_account_launcher.mjs"));
  const fromCheckout = defaultPoolLauncher("linux");
  assert.equal(fromCheckout, fileURLToPath(new URL("./claude_account_launcher.mjs", import.meta.url)));

  const fromBundle = poolProviders(existing, { ...options, launcher });
  assert.equal(fromBundle.claude_cliproxy.config.binaryPath, launcher);
  const rerun = poolProviders(fromBundle, { ...options, launcher: fromCheckout });
  assert.equal(rerun.claude_cliproxy.config.binaryPath, launcher);

  const preCutover = poolProviders(existing, options);
  assert.equal(poolProviders(preCutover, { ...options, launcher }).claude_cliproxy.config.binaryPath, options.launcher);
});

test("the pool keeps its own launch arguments, so Claude in Chrome stays on the direct profile", () => {
  const chrome = { ...existing, claudeAgent: { ...existing.claudeAgent, config: { ...existing.claudeAgent.config, launchArgs: "--chrome" } } };
  const created = poolProviders(chrome, options);
  assert.equal(created.claude_cliproxy.config.launchArgs, "");
  assert.equal(created.claude_cliproxy.config.homePath, "/existing/home");
  assert.equal(created.claude_cliproxy.config.binaryPath, options.launcher);
  assert.equal(created.claudeAgent.config.launchArgs, "--chrome");
  assert.equal(poolProviders(created, options).claude_cliproxy.config.launchArgs, "");
  created.claude_cliproxy.config.launchArgs = "--verbose";
  assert.equal(poolProviders(created, options).claude_cliproxy.config.launchArgs, "--verbose");
});

test("a pool launchArgs equal to the direct profile's is dropped, another is kept, and a new pool gets none", () => {
  const withArgs = (providers, id, launchArgs) => ({ ...providers, [id]: { ...providers[id], config: { ...providers[id].config, launchArgs } } });
  const chrome = withArgs(existing, "claudeAgent", "--chrome");
  // An earlier version copied the direct config into the pool, --chrome included.
  const inherited = withArgs(poolProviders(chrome, options), "claude_cliproxy", "--chrome");
  assert.equal(poolProviders(inherited, options).claude_cliproxy.config.launchArgs, "");
  assert.equal(poolProviders(inherited, options).claudeAgent.config.launchArgs, "--chrome");
  const own = withArgs(poolProviders(chrome, options), "claude_cliproxy", "--verbose");
  assert.equal(poolProviders(own, options).claude_cliproxy.config.launchArgs, "--verbose");
  assert.equal(poolProviders(chrome, options).claude_cliproxy.config.launchArgs, "");
  // Without direct launch arguments, an existing pool value is still the pool's own.
  const plain = withArgs(poolProviders(existing, options), "claude_cliproxy", "--verbose");
  assert.equal(poolProviders(plain, options).claude_cliproxy.config.launchArgs, "--verbose");
});

test("refuses to overwrite another provider or accept an ambiguous endpoint", () => {
  for (const id of ["claudeAgent", "named", "codex"]) assert.throws(() => poolProviders(existing, { ...options, id }), /Refusing to replace/);
  for (const upstream of ["http://user:secret@localhost", "http://localhost/v1", "http://localhost?key=secret", "file:///host"]) {
    assert.throws(() => poolProviders(existing, { ...options, upstream }), /HTTP origin/);
  }
});

test("a fallback origin is stored next to the primary, and omitting it removes it", () => {
  const withFallback = poolProviders(existing, { ...options, fallback: "http://127.0.0.1:18317/" });
  const env = (result) => Object.fromEntries(result.claude_cliproxy.environment.map((variable) => [variable.name, variable.value]));
  assert.equal(env(withFallback).CLIPROXY_FALLBACK_URL, "http://127.0.0.1:18317");
  assert.equal(withFallback.claude_cliproxy.environment.find((v) => v.name === "CLIPROXY_FALLBACK_URL").sensitive, false);
  assert.equal("CLIPROXY_FALLBACK_URL" in env(poolProviders(withFallback, options)), false);
  assert.throws(() => poolProviders(existing, { ...options, fallback: "http://127.0.0.1:8317" }), /different CLIProxy host/);
  assert.throws(() => poolProviders(existing, { ...options, fallback: "http://localhost/v1" }), /fallback must be an HTTP origin/);
});
