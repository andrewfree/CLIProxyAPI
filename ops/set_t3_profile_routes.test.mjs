import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { runCli } from "./set_t3_profile_routes.mjs";

const mac = "http://127.0.0.1:8317";
const will = "http://will.example:8317";
const binary = "/opt/homebrew/bin/codex";
const fleet = JSON.parse(fs.readFileSync(new URL("./fleet/hosts.json", import.meta.url), "utf8"));
const launcher = path.join(fleet.m5.bundle_root, "codex_fallback_launcher.mjs");
const key = "model_providers.cliproxy.base_url";
const secrets = {
  claude_lockdown_mac: "fixture-key-pro",
  claude_gmail_mac: "fixture-key-gmail",
  claude_icloud_mac: "fixture-key-icloud",
};
const launchArgs = `-c 'model_provider="cliproxy"'  --config="${key}='${mac}/v1'"\t-c mcp_servers.reading.enabled=false -c 'mcp_servers.writing.enabled=false' -c 'mcp_servers.demo.env.EXAMPLE="${mac}/v1"'`;
const env = (name, value, sensitive = false) => ({ name, value, sensitive });
const envValue = (instance, name) => instance.environment?.find((entry) => entry.name === name)?.value;
const fallbackEnv = [env("CLIPROXY_FALLBACK_URL", mac), env("CODEX_REAL_BINARY", binary)];

function instance(driver, displayName, config = {}, environment = []) {
  return { driver, displayName, enabled: true, config, environment };
}

function named(displayName, id, prefix, route) {
  return instance("claudeAgent", displayName, { binaryPath: "/fake/claude_account_launcher.mjs", launchArgs: "" }, [
    env("CLIPROXY_ACCOUNT_PREFIX", prefix),
    ...(route ? [env("CLIPROXY_CLAUDE_ROUTE", route)] : []),
    env("CLIPROXY_BASE_URL", mac),
    env("ANTHROPIC_API_KEY", secrets[id], true),
    env("ANTHROPIC_AUTH_TOKEN", ""),
  ]);
}

function fixture() {
  return {
    defaultModelSelection: {
      instanceId: "codex_cliproxy_mac", model: "gpt-6.2-sol",
      options: { reasoningEffort: "xhigh", fast: true },
    },
    fallbackModelSelection: { instanceId: "codex", model: "gpt-6.2-sol" },
    providerInstances: {
      claude_cliproxy: instance("claudeAgent", "Advanced · Claude pool · W", {}, [
        env("CLIPROXY_BASE_URL", will), env("ANTHROPIC_API_KEY", "fixture-pool-secret", true),
      ]),
      claude_cliproxy_mac: instance("claudeAgent", "Claude pool · M"),
      codex_cliproxy: instance("codex", "Advanced · CLIProxy pool · W", {
        binaryPath: launcher, launchArgs: `-c '${key}="${will}/v1"'`,
      }, [
        env("DO_NOT_TRACK", "1"), ...fallbackEnv,
      ]),
      codex_cliproxy_mac: instance("codex", "CLIProxy pool · M"),
      claude_lockdown_mac: named("Advanced · Claude Pro · M", "claude_lockdown_mac", "t3-pro", "account"),
      claude_gmail_mac: named("Advanced · Claude Gmail · M", "claude_gmail_mac", "t3-gmail"),
      claude_icloud_mac: named("Advanced · Claude iCloud Max 5× · M", "claude_icloud_mac", "t3-icloud", "account"),
      codex_kimi_mac: instance("codex", "Kimi via CLIProxy · M (lean)", { binaryPath: binary, homePath: "/fake/home", launchArgs }, [
        env("KIMI_AUTH_TOKEN", "fixture-kimi-secret", true), env("DO_NOT_TRACK", "1"),
      ]),
      other: instance("claudeAgent", "Unrelated", {}, [
        env("ANTHROPIC_API_KEY", "fixture-unflagged-key"), env("OTHER_TOKEN", "fixture-other-secret", true),
      ]),
    },
  };
}

function fakeT3(settings, { onWrite, onRead, redact = true } = {}) {
  const saved = structuredClone(settings);
  const writes = [];
  const sessions = [];
  let closed = 0;
  let reads = 0;
  return {
    saved, writes, sessions, closed: () => closed,
    async connect(baseDir, scopes) {
      sessions.push({ baseDir, scopes });
      return {
        close() { closed++; },
        async rpc(tag, payload) {
          if (tag === "server.getConfig") {
            onRead?.(saved, ++reads);
            const settings = structuredClone(saved);
            if (redact) for (const instance of Object.values(settings.providerInstances)) {
              for (const entry of instance.environment ?? []) {
                if (entry.sensitive && entry.value) Object.assign(entry, { value: "", valueRedacted: true });
              }
            }
            return { settings };
          }
          assert.equal(tag, "server.updateSettings");
          const { patch } = structuredClone(payload);
          writes.push(structuredClone(patch));
          // The server replaces the provider map as a whole and preserves echoed redaction markers.
          for (const [id, updated] of Object.entries(patch.providerInstances ?? {})) {
            for (const entry of updated.environment ?? []) {
              if (entry.sensitive && entry.valueRedacted) {
                entry.value = envValue(saved.providerInstances[id], entry.name);
                delete entry.valueRedacted;
              }
            }
          }
          Object.assign(saved, patch);
          onWrite?.(saved);
        },
      };
    },
  };
}

function fixtureFetch(statuses = {}, gmailModel = "t3-gmail/claude-opus") {
  const requests = [];
  return {
    requests,
    async fetchModels(url, options) {
      const id = Object.keys(secrets).find((id) => options.headers.authorization === `Bearer ${secrets[id]}`);
      requests.push({ url, id, method: options.method, redirect: options.redirect });
      if (!id) throw new Error("fixture-secret-in-network-error");
      const status = statuses[id] ?? 200;
      if (status === "invalid-response") return new Response("Not a model list");
      return new Response(JSON.stringify({ data: [
        { id: "t3-pro/claude-opus" }, { id: "t3-icloud/claude-sonnet" }, { id: gmailModel },
      ] }), { status });
    },
  };
}

function harness(t, settings = fixture(), options = {}) {
  const baseDir = fs.mkdtempSync(path.join(os.tmpdir(), "t3-profile-routes-"));
  t.after(() => fs.rmSync(baseDir, { recursive: true, force: true }));
  const backupDir = path.join(baseDir, "backup");
  const service = fakeT3(settings, options);
  const network = fixtureFetch(options.statuses, options.gmailModel);
  async function run(mode, args = []) {
    const lines = [];
    const argv = mode ? [mode] : [];
    argv.push("--base-dir", baseDir);
    if (mode !== "rollback") argv.push("--will-origin", `${will}/`);
    argv.push(...args);
    const code = await runCli(argv, {
      connect: service.connect,
      fetchModels: network.fetchModels,
      readKeys: () => secrets,
      print: (line) => lines.push(line),
    });
    assert.equal(lines.length, 1);
    assert.doesNotMatch(lines[0], /fixture-(?:key|.*secret|unflagged)/);
    return { code, report: JSON.parse(lines[0]) };
  }
  return {
    ...service, ...network, backupDir, run,
    apply: (steps, extra = []) => run("apply", ["--steps", steps, "--backup-dir", backupDir, ...extra]),
    rollback: (record) => run("rollback", ["--record", record]),
  };
}

test("the default invocation prints all five steps without writing or checking keys", async (t) => {
  const h = harness(t);
  const { code, report } = await h.run(null);
  assert.equal(code, 0);
  assert.equal(report.mode, "plan");
  assert.deepEqual(Object.keys(report.steps), ["default", "names", "mac-pool-name", "named-accounts", "kimi-fallback"]);
  assert.deepEqual(report.steps.default, [{
    field: "defaultModelSelection.instanceId", from: "codex_cliproxy_mac", to: "codex_cliproxy", needed: true,
  }]);
  assert.deepEqual(report.steps.names.map(({ field }) => field), [
    "claude_cliproxy.displayName", "codex_cliproxy.displayName", "codex_cliproxy_mac.displayName",
  ]);
  assert.equal(report.steps["mac-pool-name"][0].to, "Advanced · Claude pool · M (Mac only)");
  assert.equal(report.steps["named-accounts"][1].to, will);
  assert.deepEqual(report.steps["named-accounts"].filter(({ field }) => field.endsWith(".displayName")).map(({ field }) => field), [
    "claude_lockdown_mac.displayName", "claude_gmail_mac.displayName", "claude_icloud_mac.displayName",
  ]);
  assert.equal(report.steps["kimi-fallback"][1].to, `${will}/v1`);
  assert.equal(report.steps["kimi-fallback"].find(({ field }) => field === "codex_kimi_mac.displayName").to, "Kimi via CLIProxy · W (lean)");
  assert.deepEqual(report.macReferences, []);
  assert.deepEqual(h.sessions[0].scopes, ["orchestration:read"]);
  assert.deepEqual(h.requests, []);
  assert.deepEqual(h.writes, []);
  assert.equal(fs.existsSync(h.backupDir), false);
  assert.equal(h.closed(), 1);
});

test("plan and apply report every other top-level and project Mac model selection without editing it", async (t) => {
  const expected = fixture();
  const codexMac = { instanceId: "codex_cliproxy_mac", model: "gpt-6.2-sol" };
  const claudeMac = { instanceId: "claude_cliproxy_mac", model: "claude-sonnet" };
  expected.fallbackModelSelection = codexMac;
  expected.reviewModelSelection = claudeMac;
  expected.unrelated = claudeMac;
  expected.projectSettingsOverrides = {
    "project.with dots": { defaultModelSelection: claudeMac, fallbackModelSelection: codexMac, unrelated: codexMac },
    other: { customModelSelection: claudeMac, defaultModelSelection: { instanceId: "codex_cliproxy" } },
  };
  const h = harness(t, expected);
  const references = [
    { path: "fallbackModelSelection", instanceId: "codex_cliproxy_mac" },
    { path: "reviewModelSelection", instanceId: "claude_cliproxy_mac" },
    { path: 'projectSettingsOverrides["project.with dots"].defaultModelSelection', instanceId: "claude_cliproxy_mac" },
    { path: 'projectSettingsOverrides["project.with dots"].fallbackModelSelection', instanceId: "codex_cliproxy_mac" },
    { path: 'projectSettingsOverrides["other"].customModelSelection', instanceId: "claude_cliproxy_mac" },
  ];
  const plan = await h.run("plan");
  assert.equal(plan.code, 0);
  assert.deepEqual(plan.report.macReferences, references);
  const applied = await h.apply("default,names");
  assert.equal(applied.code, 0);
  assert.deepEqual(applied.report.macReferences, references);
  assert.deepEqual(Object.keys(h.writes[0]), ["defaultModelSelection", "providerInstances"]);
  assert.deepEqual(h.saved.projectSettingsOverrides, expected.projectSettingsOverrides);
  assert.deepEqual(h.saved.fallbackModelSelection, expected.fallbackModelSelection);
  assert.deepEqual(h.saved.reviewModelSelection, expected.reviewModelSelection);
});

test("both route steps require the supplied Will origin to match both existing pools in plan and apply", async (t) => {
  const cases = [
    [(s) => { s.providerInstances.codex_cliproxy.config.launchArgs = `-c '${key}="${mac}/v1"'`; }, /must match.*codex_cliproxy/],
    [(s) => { s.providerInstances.codex_cliproxy.config.launchArgs = `-c '${key}="invalid-url"'`; }, /codex_cliproxy launchArgs.*valid/],
    [(s) => { s.providerInstances.claude_cliproxy.environment[0].value = mac; }, /must match.*claude_cliproxy/],
  ];
  for (const [edit, message] of cases) {
    const settings = fixture();
    edit(settings);
    const h = harness(t, settings);
    const plan = await h.run("plan");
    assert.equal(plan.code, 1);
    for (const step of ["named-accounts", "kimi-fallback"]) {
      assert.match(plan.report.steps[step].error, message);
      const applied = await h.apply(step);
      assert.equal(applied.code, 1);
      assert.match(applied.report.steps[step].error, message);
    }
    assert.deepEqual(h.writes, []);
    assert.deepEqual(h.requests, []);
    assert.equal(fs.existsSync(h.backupDir), false);
    assert.equal((await h.apply("names")).code, 0);
  }
});

test("default patches the full selection and keeps its model, options and other settings", async (t) => {
  const expected = fixture();
  const h = harness(t, expected);
  expected.defaultModelSelection.instanceId = "codex_cliproxy";
  const { code, report } = await h.apply("default");
  assert.equal(code, 0);
  assert.deepEqual(h.saved, expected);
  assert.deepEqual(h.writes, [{ defaultModelSelection: expected.defaultModelSelection }]);
  const record = JSON.parse(fs.readFileSync(report.record, "utf8"));
  assert.deepEqual(record.changes, [{
    step: "default", field: "defaultModelSelection.instanceId", from: "codex_cliproxy_mac", to: "codex_cliproxy",
  }]);
});

test("names changes only the three specified pool labels, and the Mac Claude pool has a separate step", async (t) => {
  const expected = fixture();
  const h = harness(t, expected);
  const names = {
    claude_cliproxy: "Claude pool · W",
    codex_cliproxy: "CLIProxy pool · W",
    codex_cliproxy_mac: "Advanced · CLIProxy pool · M (Mac only)",
  };
  for (const [id, name] of Object.entries(names)) expected.providerInstances[id].displayName = name;
  assert.equal((await h.apply("names")).code, 0);
  assert.deepEqual(h.saved, expected);
  expected.providerInstances.claude_cliproxy_mac.displayName = "Advanced · Claude pool · M (Mac only)";
  assert.equal((await h.apply("mac-pool-name")).code, 0);
  assert.deepEqual(h.saved, expected);
});

const willAccountNames = {
  claude_lockdown_mac: "Advanced · Claude Pro · W",
  claude_gmail_mac: "Advanced · Claude Gmail · W",
  claude_icloud_mac: "Advanced · Claude iCloud Max 5× · W",
};

test("named accounts retain the pinned prefixes and stored keys and probe each key on Will", async (t) => {
  const expected = fixture();
  const h = harness(t, expected);
  const { code, report } = await h.apply("named-accounts", ["--named-account-keys-stdin"]);
  assert.equal(code, 0);
  assert.deepEqual(report.keyChecks, [
    { id: "claude_lockdown_mac", prefix: "t3-pro", status: 200, prefixPresent: true },
    { id: "claude_gmail_mac", prefix: "t3-gmail", status: 200, prefixPresent: true },
    { id: "claude_icloud_mac", prefix: "t3-icloud", status: 200, prefixPresent: true },
  ]);
  assert.deepEqual(h.requests, Object.keys(secrets).map((id) => ({ id, url: `${will}/v1/models`, method: "GET", redirect: "manual" })));
  for (const id of Object.keys(secrets)) {
    expected.providerInstances[id].displayName = willAccountNames[id];
    expected.providerInstances[id].environment.find((entry) => entry.name === "CLIPROXY_BASE_URL").value = will;
    const sentKey = h.writes[0].providerInstances[id].environment.find(({ name }) => name === "ANTHROPIC_API_KEY");
    assert.deepEqual(sentKey, { name: "ANTHROPIC_API_KEY", value: "", sensitive: true, valueRedacted: true });
  }
  assert.deepEqual(h.saved, expected);
});

test("an explicitly empty named-account fallback survives apply and rollback", async (t) => {
  const settings = fixture();
  settings.providerInstances.claude_lockdown_mac.environment.push(env("CLIPROXY_FALLBACK_URL", ""));
  const h = harness(t, settings);
  const { code, report } = await h.apply("named-accounts", ["--named-account-keys-stdin"]);
  assert.equal(code, 0);
  assert.equal(envValue(h.saved.providerInstances.claude_lockdown_mac, "CLIPROXY_FALLBACK_URL"), "");
  assert.equal((await h.rollback(report.record)).code, 0);
  assert.deepEqual(h.saved, settings);
});

test("--no-key-check applies named accounts without probing keys, and only with that step", async (t) => {
  const expected = fixture();
  const h = harness(t, expected);
  const { code, report } = await h.apply("named-accounts", ["--no-key-check"]);
  assert.equal(code, 0);
  assert.equal(report.keyChecks, "skipped");
  assert.deepEqual(h.requests, []);
  for (const id of Object.keys(secrets)) {
    expected.providerInstances[id].displayName = willAccountNames[id];
    expected.providerInstances[id].environment.find((entry) => entry.name === "CLIPROXY_BASE_URL").value = will;
  }
  assert.deepEqual(h.saved, expected);

  const other = harness(t);
  const wrongStep = await other.apply("names", ["--no-key-check"]);
  assert.equal(wrongStep.code, 1);
  assert.match(wrongStep.report.error, /require the named-accounts step/);
  const both = await other.apply("named-accounts", ["--no-key-check", "--named-account-keys-stdin"]);
  assert.equal(both.code, 1);
  assert.match(both.report.error, /not both/);
  assert.deepEqual(other.writes, []);
});

test("Kimi uses the same launcher shape as the Will pool and edits only the intended URL bytes", async (t) => {
  const expected = fixture();
  const h = harness(t, expected);
  const { code, report } = await h.apply("kimi-fallback");
  assert.equal(code, 0);
  const kimi = expected.providerInstances.codex_kimi_mac;
  kimi.displayName = "Kimi via CLIProxy · W (lean)";
  kimi.config.binaryPath = launcher;
  kimi.config.launchArgs = launchArgs.replace(`${key}='${mac}/v1'`, `${key}='${will}/v1'`);
  kimi.environment.push(...fallbackEnv);
  assert.deepEqual(h.saved, expected);
  const record = fs.readFileSync(report.record, "utf8");
  assert.doesNotMatch(record, /mcp_servers|KIMI_AUTH_TOKEN/);
  assert.match(record, /CLIPROXY_FALLBACK_URL/);
});

const allSteps = "default,names,mac-pool-name,named-accounts,kimi-fallback";
test("reapplying all steps does not write settings, probe keys or create more receipts", async (t) => {
  const h = harness(t);
  assert.equal((await h.apply(allSteps, ["--named-account-keys-stdin"])).code, 0);
  const { code, report } = await h.apply(allSteps);
  assert.equal(code, 0);
  assert.ok(Object.values(report.steps).flat().every((field) => !field.needed));
  assert.equal(h.writes.length, 1);
  assert.equal(h.requests.length, 3);
  assert.equal(fs.readdirSync(h.backupDir).length, 2);
});

test("unknown or unsupported starting values refuse the entire selected patch", async (t) => {
  const cases = [
    ["default", (s) => { delete s.defaultModelSelection; }, /populated settings.defaultModelSelection/],
    ["default", (s) => { s.defaultModelSelection.instanceId = "codex"; }, /defaultModelSelection.instanceId: unexpected/],
    ["names", (s) => { s.providerInstances.codex_cliproxy.displayName = "fixture-name-secret"; }, /codex_cliproxy.displayName: unexpected/],
    ["mac-pool-name", (s) => { s.providerInstances.claude_cliproxy_mac.displayName = "Custom"; }, /claude_cliproxy_mac.displayName: unexpected/],
    ["named-accounts", (s) => { s.providerInstances.claude_lockdown_mac.environment[0].value = "different-account"; }, /pinned account/],
    ["named-accounts", (s) => { s.providerInstances.claude_lockdown_mac.environment[1].value = "shared-pool"; }, /pinned account/],
    ["named-accounts", (s) => { s.providerInstances.claude_lockdown_mac.environment.push(fallbackEnv[0]); }, /no fallback/],
    ["named-accounts", (s) => { s.providerInstances.claude_lockdown_mac.environment[2].sensitive = true; }, /non-sensitive value/],
    ["named-accounts", (s) => { s.providerInstances.claude_gmail_mac.displayName = "Custom"; }, /claude_gmail_mac.displayName: unexpected/],
    ["kimi-fallback", (s) => { s.providerInstances.codex_cliproxy.config.binaryPath = binary; }, /expected fallback launcher and environment/],
    ["kimi-fallback", (s) => { s.providerInstances.codex_cliproxy.environment.pop(); }, /expected fallback launcher and environment/],
    ["kimi-fallback", (s) => { s.providerInstances.codex_kimi_mac.environment.push(env("CODEX_REAL_BINARY", "/different")); }, /CODEX_REAL_BINARY: unexpected/],
    ["kimi-fallback", (s) => { s.providerInstances.codex_kimi_mac.config.launchArgs += ` -c '${key}="${mac}/v1"'`; }, /unambiguous/],
    ["kimi-fallback", (s) => { s.providerInstances.codex_kimi_mac.config.launchArgs += String.raw` -c 'model_providers.cliproxy."base_\u0075rl"="${mac}/v1"'`; }, /unambiguous/],
    ["kimi-fallback", (s) => { s.providerInstances.codex_kimi_mac.environment.push(env("T3CODE_CODEX_LAUNCH_ARGS", "override")); }, /overrides config.launchArgs/],
    ["kimi-fallback", (s) => { s.providerInstances.codex_kimi_mac.displayName = "Custom"; }, /codex_kimi_mac.displayName: unexpected/],
  ];
  for (const [step, change, message] of cases) {
    const settings = fixture();
    change(settings);
    const h = harness(t, settings);
    const { code, report } = await h.apply(step === "default" ? "default,names" : `${step},default`);
    assert.equal(code, 1);
    assert.match(report.steps[step].error, message);
    assert.deepEqual(h.writes, []);
    assert.deepEqual(h.requests, []);
    assert.equal(fs.existsSync(h.backupDir), false);
  }
});

test("a refused key or a redacted key unavailable for checking blocks every requested write", async (t) => {
  const h = harness(t, fixture(), { statuses: { claude_gmail_mac: 401 } });
  const failed = await h.apply("names,named-accounts", ["--named-account-keys-stdin"]);
  assert.equal(failed.code, 1);
  assert.match(failed.report.error, /must receive HTTP 200/);
  assert.equal(failed.report.keyChecks[1].status, 401);
  assert.equal(failed.report.keyChecks[1].prefixPresent, null);
  assert.equal(h.requests.length, 3);
  const missing = await h.apply("named-accounts");
  assert.equal(missing.code, 1);
  assert.match(missing.report.keyChecks[0].error, /redacted key/);
  assert.equal(h.requests.length, 3);
  assert.deepEqual(h.writes, []);
  assert.equal(fs.existsSync(h.backupDir), false);
});

test("HTTP 200 from a route without a model list cannot authorize the named-account edit", async (t) => {
  const h = harness(t, fixture(), { statuses: { claude_icloud_mac: "invalid-response" } });
  const { code, report } = await h.apply("named-accounts", ["--named-account-keys-stdin"]);
  assert.equal(code, 1);
  assert.match(report.error, /expected prefix/);
  assert.equal(report.keyChecks[2].status, 200);
  assert.equal(report.keyChecks[2].prefixPresent, null);
  assert.deepEqual(h.writes, []);
});

test("HTTP 200 with only a similarly named Gmail prefix refuses the entire apply", async (t) => {
  const h = harness(t, fixture(), { gmailModel: "t3-gmail-extra/claude-opus" });
  const { code, report } = await h.apply("names,named-accounts", ["--named-account-keys-stdin"]);
  assert.equal(code, 1);
  assert.match(report.error, /expected prefix/);
  assert.deepEqual(report.keyChecks[1], { id: "claude_gmail_mac", prefix: "t3-gmail", status: 200, prefixPresent: false });
  assert.deepEqual(h.writes, []);
  assert.equal(fs.existsSync(h.backupDir), false);
});

test("apply writes a private redacted settings copy and a minimal rollback record before the RPC", async (t) => {
  let h;
  h = harness(t, fixture(), { redact: false, onWrite() {
    assert.equal(fs.readdirSync(h.backupDir).length, 2);
  } });
  const { code, report } = await h.apply("named-accounts");
  assert.equal(code, 0);
  assert.equal(h.requests.length, 3);
  for (const file of [report.backup, report.record]) {
    assert.equal(fs.statSync(file).mode & 0o777, 0o600);
    assert.doesNotMatch(fs.readFileSync(file, "utf8"), /fixture-(?:key|.*secret|unflagged)/);
  }
  const saved = JSON.parse(fs.readFileSync(report.backup, "utf8"));
  assert.deepEqual(saved.defaultModelSelection, fixture().defaultModelSelection);
  assert.equal(envValue(saved.providerInstances.other, "ANTHROPIC_API_KEY"), "<redacted>");
  assert.equal(envValue(saved.providerInstances.other, "OTHER_TOKEN"), "<redacted>");
  const record = JSON.parse(fs.readFileSync(report.record, "utf8"));
  assert.deepEqual(record.changes, [
    ...Object.keys(secrets).map((id) => ({ step: "named-accounts", field: `${id}.environment.CLIPROXY_BASE_URL`, from: mac, to: will })),
    ...Object.entries(willAccountNames).map(([id, to]) => ({
      step: "named-accounts", field: `${id}.displayName`, from: fixture().providerInstances[id].displayName, to,
    })),
  ]);
});

test("rollback restores only recorded changes and leaves later unrelated changes alone", async (t) => {
  const before = fixture();
  before.providerInstances.claude_cliproxy.displayName = "Claude pool · W";
  delete before.providerInstances.codex_kimi_mac.environment;
  const h = harness(t, before);
  const { code, report } = await h.apply(allSteps, ["--named-account-keys-stdin"]);
  assert.equal(code, 0);
  h.saved.providerInstances.other.displayName = "Updated later";
  h.saved.defaultModelSelection.options.fast = false;
  h.saved.providerInstances.codex_kimi_mac.config.launchArgs += " -c mcp_servers.extra.enabled=false";
  assert.equal((await h.rollback(report.record)).code, 0);
  before.providerInstances.other.displayName = "Updated later";
  before.defaultModelSelection.options.fast = false;
  before.providerInstances.codex_kimi_mac.config.launchArgs += " -c mcp_servers.extra.enabled=false";
  assert.deepEqual(h.saved, before);
  assert.equal((await h.rollback(report.record)).code, 0);
  assert.equal(h.writes.length, 2);
  assert.equal(h.requests.length, 3);
});

test("rollback refuses unexpected state and records that request fields outside the plan", async (t) => {
  const h = harness(t);
  const { report } = await h.apply("names");
  h.saved.providerInstances.codex_cliproxy.displayName = "Edited later";
  const refused = await h.rollback(report.record);
  assert.equal(refused.code, 1);
  assert.match(refused.report.steps.names.error, /codex_cliproxy.displayName: unexpected/);
  const tampered = JSON.parse(fs.readFileSync(report.record, "utf8"));
  tampered.changes[0].field = "claude_cliproxy.environment.ANTHROPIC_API_KEY";
  fs.writeFileSync(report.record, JSON.stringify(tampered));
  assert.match((await h.rollback(report.record)).report.error, /Invalid rollback record/);
  assert.equal(h.writes.length, 1);
});

test("rollback retains the original step grouping for old records and enforces the new grouping for new records", async (t) => {
  const settings = fixture();
  const h = harness(t, settings);
  const { code, report } = await h.apply("named-accounts,kimi-fallback", ["--no-key-check"]);
  assert.equal(code, 0);
  const record = JSON.parse(fs.readFileSync(report.record, "utf8"));
  assert.equal(record.version, 2);
  for (const change of record.changes) {
    if (change.field.endsWith(".displayName")) change.step = "names";
  }
  fs.writeFileSync(report.record, JSON.stringify(record));
  assert.match((await h.rollback(report.record)).report.error, /Invalid rollback record/);
  assert.equal(h.writes.length, 1);
  record.version = 1;
  fs.writeFileSync(report.record, JSON.stringify(record));
  assert.equal((await h.rollback(report.record)).code, 0);
  assert.deepEqual(h.saved, settings);
});

test("preflight refuses settings drift before sending a replacement map", async (t) => {
  const h = harness(t, fixture(), { onRead(saved, count) {
    if (count === 2) saved.providerInstances.other.displayName = "Concurrent edit";
  } });
  const { code, report } = await h.apply("names");
  assert.equal(code, 1);
  assert.match(report.error, /changed during preflight/);
  assert.deepEqual(h.writes, []);
});

test("readback mismatch returns a failing exit code and keeps the rollback record", async (t) => {
  const h = harness(t, fixture(), { onWrite(saved) {
    saved.defaultModelSelection.instanceId = "codex_cliproxy_mac";
  } });
  const { code, report } = await h.apply("default");
  assert.equal(code, 1);
  assert.equal(report.ok, false);
  assert.match(report.error, /on readback/);
  assert.deepEqual(report.mismatches, ["defaultModelSelection"]);
  assert.ok(fs.existsSync(report.record));
  assert.equal(h.closed(), 1);
});

test("the command exits 1 and prints one safe JSON report for invalid arguments", () => {
  const result = spawnSync(process.execPath, [
    new URL("./set_t3_profile_routes.mjs", import.meta.url).pathname, "--fixture-secret",
  ], { encoding: "utf8" });
  assert.equal(result.status, 1);
  assert.equal(result.stderr, "");
  assert.match(JSON.parse(result.stdout).error, /^Usage:/);
  assert.doesNotMatch(result.stdout, /fixture-secret/);
});
