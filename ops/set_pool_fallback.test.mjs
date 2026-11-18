import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { poolFallbackEnvironment } from "./set_pool_fallback.mjs";
import { poolProviders } from "./configure_claude_pool.mjs";

const tool = fileURLToPath(new URL("./set_pool_fallback.mjs", import.meta.url));
const primary = "http://will.example:8317";
const fallback = "http://127.0.0.1:8317";
const apiKey = "fixture-key";

// A pool profile environment as server.getConfig returns it, with the key redacted.
const saved = [
  { name: "CLIPROXY_CLAUDE_ROUTE", value: "shared-pool", sensitive: false },
  { name: "CLIPROXY_ACCOUNT_PREFIX", value: "", sensitive: false },
  { name: "CLIPROXY_BASE_URL", value: "http://127.0.0.1:18317", sensitive: false },
  { name: "CLIPROXY_CLAUDE_BINARY", value: "/bin/claude", sensitive: false },
  { name: "ANTHROPIC_API_KEY", value: "", sensitive: true, valueRedacted: true },
  { name: "ANTHROPIC_AUTH_TOKEN", value: "", sensitive: false },
  { name: "CLAUDE_CODE_OAUTH_TOKEN", value: "", sensitive: false },
  { name: "DO_NOT_TRACK", value: "1", sensitive: false },
  { name: "CLIPROXY_CONNECT_TIMEOUT_MS", value: "3000", sensitive: false },
];
const names = (environment) => environment.map((variable) => variable.name);
const value = (environment, name) => environment.find((variable) => variable.name === name)?.value;

test("setting a fallback puts it right after the primary and keeps every other variable in order", () => {
  const before = structuredClone(saved);
  const result = poolFallbackEnvironment(saved, { primary: `${primary}/`, fallback: `${fallback}/`, apiKey: ` ${apiKey}\n` });
  assert.deepEqual(saved, before);
  const expected = names(saved);
  expected.splice(expected.indexOf("CLIPROXY_BASE_URL") + 1, 0, "CLIPROXY_FALLBACK_URL");
  assert.deepEqual(names(result), expected);
  assert.deepEqual(result.find((v) => v.name === "CLIPROXY_BASE_URL"), { name: "CLIPROXY_BASE_URL", value: primary, sensitive: false });
  assert.deepEqual(result.find((v) => v.name === "CLIPROXY_FALLBACK_URL"), { name: "CLIPROXY_FALLBACK_URL", value: fallback, sensitive: false });
  assert.deepEqual(result.find((v) => v.name === "ANTHROPIC_API_KEY"), { name: "ANTHROPIC_API_KEY", value: apiKey, sensitive: true });
  for (const name of ["CLIPROXY_CLAUDE_BINARY", "DO_NOT_TRACK", "CLIPROXY_CONNECT_TIMEOUT_MS"]) {
    assert.deepEqual(result.find((v) => v.name === name), saved.find((v) => v.name === name));
  }
});

test("a new fallback replaces the old one wherever it was, and reapplying changes nothing", () => {
  const misplaced = [...saved, { name: "CLIPROXY_FALLBACK_URL", value: "http://old.example:8317", sensitive: false }];
  const result = poolFallbackEnvironment(misplaced, { primary, fallback, apiKey });
  assert.equal(names(result).filter((name) => name === "CLIPROXY_FALLBACK_URL").length, 1);
  assert.equal(names(result).indexOf("CLIPROXY_FALLBACK_URL"), names(result).indexOf("CLIPROXY_BASE_URL") + 1);
  assert.equal(value(result, "CLIPROXY_FALLBACK_URL"), fallback);
  assert.deepEqual(poolFallbackEnvironment(result, { primary, fallback, apiKey }), result);
});

test("a null fallback removes it and leaves the rest in order", () => {
  const withFallback = poolFallbackEnvironment(saved, { primary, fallback, apiKey });
  const result = poolFallbackEnvironment(withFallback, { primary, fallback: null, apiKey });
  assert.deepEqual(names(result), names(saved));
  assert.equal(value(result, "CLIPROXY_BASE_URL"), primary);
  assert.equal(value(result, "ANTHROPIC_API_KEY"), apiKey);
});

test("the result matches the layout the pool configurator writes", () => {
  const providers = { claudeAgent: { driver: "claudeAgent", config: { binaryPath: "/bin/claude" } } };
  const options = { id: "claude_cliproxy", label: "Pool", upstream: primary, launcher: "/repo/ops/claude_account_launcher.mjs", binary: "/bin/claude", apiKey };
  const plain = poolProviders(providers, options).claude_cliproxy.environment;
  const configured = poolProviders(providers, { ...options, fallback }).claude_cliproxy.environment;
  assert.deepEqual(poolFallbackEnvironment(plain, { primary, fallback, apiKey }), configured);
  assert.deepEqual(poolFallbackEnvironment(configured, { primary, fallback: null, apiKey }), plain);
});

test("refuses profiles that are not shared pools or cannot keep their key", () => {
  const options = { primary, fallback, apiKey };
  const named = saved.filter((v) => v.name !== "CLIPROXY_CLAUDE_ROUTE").map((v) => (v.name === "CLIPROXY_ACCOUNT_PREFIX" ? { ...v, value: "t3-pro" } : v));
  assert.throws(() => poolFallbackEnvironment(named, options), /not a shared Claude pool/);
  assert.throws(() => poolFallbackEnvironment(saved.map((v) => (v.name === "CLIPROXY_CLAUDE_ROUTE" ? { ...v, value: "named" } : v)), options), /not a shared Claude pool/);
  assert.throws(() => poolFallbackEnvironment([], options), /not a shared Claude pool/);
  assert.throws(() => poolFallbackEnvironment(saved.filter((v) => v.name !== "ANTHROPIC_API_KEY"), options), /without ANTHROPIC_API_KEY/);
  assert.throws(() => poolFallbackEnvironment(saved.filter((v) => v.name !== "CLIPROXY_BASE_URL"), options), /without CLIPROXY_BASE_URL/);
  for (const key of ["", "  \n", undefined]) assert.throws(() => poolFallbackEnvironment(saved, { ...options, apiKey: key }), /API key is required/);
});

test("refuses a fallback equal to the primary, ambiguous origins, and an unstated fallback", () => {
  const options = { primary, fallback, apiKey };
  assert.throws(() => poolFallbackEnvironment(saved, { ...options, fallback: `${primary}/` }), /different CLIProxy host/);
  for (const bad of ["http://user:secret@localhost", "http://localhost/v1", "http://localhost?key=secret", "http://localhost/#x", "file:///host", "not a url"]) {
    assert.throws(() => poolFallbackEnvironment(saved, { ...options, primary: bad }), /primary must be an HTTP origin/);
    assert.throws(() => poolFallbackEnvironment(saved, { ...options, fallback: bad }), /fallback must be an HTTP origin/);
  }
  assert.throws(() => poolFallbackEnvironment(saved, { primary, apiKey }), /null to remove it/);
});

test("the CLI rejects bad arguments and prints no key when T3 is unreachable", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "set-pool-fallback-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const backupDir = path.join(dir, "backup");
  const args = ["--base-dir", path.join(dir, "t3"), "--profile-id", "claude_cliproxy", "--primary", primary, "--backup-dir", backupDir];
  const run = (extra, input = apiKey) => spawnSync(process.execPath, [tool, ...extra], { input, encoding: "utf8" });

  const missing = run(["--profile-id", "claude_cliproxy"]);
  assert.equal(missing.status, 1);
  assert.match(missing.stderr, /--base-dir is required/);
  const both = run([...args, "--fallback", fallback, "--no-fallback"]);
  assert.equal(both.status, 1);
  assert.match(both.stderr, /exactly one of --fallback/);
  const stray = run([...args, "--no-fallback", "stray-secret-value"]);
  assert.equal(stray.status, 1);
  assert.match(stray.stderr, /^Usage:/);
  assert.doesNotMatch(stray.stderr, /stray-secret-value/);

  const unreachable = run([...args, "--fallback", fallback]);
  assert.equal(unreachable.status, 1);
  assert.match(unreachable.stderr, /no secret values are printed/);
  assert.doesNotMatch(unreachable.stdout + unreachable.stderr, /fixture-key/);
  assert.deepEqual(fs.readdirSync(backupDir), []);
});
