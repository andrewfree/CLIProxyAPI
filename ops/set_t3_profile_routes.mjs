#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { isDeepStrictEqual, parseArgs } from "node:util";
import { pathToFileURL } from "node:url";
import { connectT3, httpOrigin, withoutSecrets } from "./configure_claude_pool.mjs";

const macOrigin = "http://127.0.0.1:8317";
const codexBinary = "/opt/homebrew/bin/codex";
const baseURLKey = "model_providers.cliproxy.base_url";
const { m5 } = JSON.parse(fs.readFileSync(new URL("./fleet/hosts.json", import.meta.url), "utf8"));
const codexLauncher = path.join(m5.bundle_root, "codex_fallback_launcher.mjs");

const accountPrefixes = {
  claude_lockdown_mac: "t3-pro",
  claude_gmail_mac: "t3-gmail",
  claude_icloud_mac: "t3-icloud",
};
const fallbackEnv = {
  CLIPROXY_FALLBACK_URL: macOrigin,
  CODEX_REAL_BINARY: codexBinary,
};

const field = (id, kind, name, from, to) => ({ id, kind, name, from, to });
const displayName = (id, from, to) => field(id, "profile", "displayName", from, to);
function routePlan(willOrigin) {
  return {
    default: [field(null, "default", "instanceId", "codex_cliproxy_mac", "codex_cliproxy")],
    // T3's PR review guide runs on its own model selection, which pointed at the Mac-only profile.
    "review-guide": [field(null, "selection", "reviewGuideModelSelection", "codex_cliproxy_mac", "codex_cliproxy")],
    names: [
      displayName("claude_cliproxy", "Advanced · Claude pool · W", "Claude pool · W"),
      displayName("codex_cliproxy", "Advanced · CLIProxy pool · W", "CLIProxy pool · W"),
      displayName("codex_cliproxy_mac", "CLIProxy pool · M", "Advanced · CLIProxy pool · M (Mac only)"),
    ],
    "mac-pool-name": [
      displayName("claude_cliproxy_mac", "Claude pool · M", "Advanced · Claude pool · M (Mac only)"),
    ],
    "named-accounts": [
      ...Object.keys(accountPrefixes).map((id) => field(id, "environment", "CLIPROXY_BASE_URL", macOrigin, willOrigin)),
      displayName("claude_lockdown_mac", "Advanced · Claude Pro · M", "Advanced · Claude Pro · W"),
      displayName("claude_gmail_mac", "Advanced · Claude Gmail · M", "Advanced · Claude Gmail · W"),
      displayName("claude_icloud_mac", "Advanced · Claude iCloud Max 5× · M", "Advanced · Claude iCloud Max 5× · W"),
    ],
    "kimi-fallback": [
      field("codex_kimi_mac", "config", "binaryPath", codexBinary, codexLauncher),
      field("codex_kimi_mac", "launchArgs", baseURLKey, `${macOrigin}/v1`, `${willOrigin}/v1`),
      ...Object.entries(fallbackEnv).map(([name, value]) => field("codex_kimi_mac", "environment", name, null, value)),
      displayName("codex_kimi_mac", "Kimi via CLIProxy · M (lean)", "Kimi via CLIProxy · W (lean)"),
    ],
  };
}

const usage = [
  "Usage: set_t3_profile_routes.mjs [plan] --base-dir <dir> --will-origin <origin>",
  "       set_t3_profile_routes.mjs apply --base-dir <dir> --will-origin <origin> --steps <comma-separated list> --backup-dir <dir> [--named-account-keys-stdin | --no-key-check]",
  "       set_t3_profile_routes.mjs rollback --base-dir <dir> --record <file> [--steps <comma-separated list>]",
  "For redacted named-account keys, pass a JSON object keyed by profile ID on stdin. The keys are used only for verification.",
].join("\n");

class RouteError extends Error {}

function willHTTPOrigin(value) {
  let origin;
  try {
    origin = httpOrigin(value, "Will origin");
  } catch {
    throw new RouteError("--will-origin must be an HTTP origin without credentials or a path.");
  }
  if (origin === macOrigin || /["'\\]/.test(origin)) throw new RouteError("--will-origin must name a different proxy with a valid origin.");
  return origin;
}

function options(args) {
  let parsed;
  try {
    parsed = parseArgs({
      args, allowPositionals: true,
      options: {
        "base-dir": { type: "string" }, "will-origin": { type: "string" }, steps: { type: "string" },
        "backup-dir": { type: "string" }, record: { type: "string" }, "named-account-keys-stdin": { type: "boolean" },
        "no-key-check": { type: "boolean" },
      },
    });
  } catch {
    throw new RouteError(usage);
  }
  const { values, positionals: [mode = "plan", ...extra] } = parsed;
  const flags = {
    plan: ["base-dir", "will-origin"],
    apply: ["base-dir", "will-origin", "steps", "backup-dir"],
    rollback: ["base-dir", "record"],
  };
  if (extra.length || !Object.hasOwn(flags, mode)) throw new RouteError(usage);
  const required = flags[mode];
  const optional = { apply: ["named-account-keys-stdin", "no-key-check"], rollback: ["steps"] };
  const accepted = [...required, ...(optional[mode] ?? [])];
  if (required.some((name) => !values[name]) || Object.keys(values).some((name) => !accepted.includes(name))) {
    throw new RouteError(usage);
  }
  const steps = values.steps?.split(",").map((step) => step.trim());
  if (steps && (new Set(steps).size !== steps.length || steps.some((step) => !Object.hasOwn(routePlan(), step)))) {
    throw new RouteError("Pass unique step names: default, review-guide, names, mac-pool-name, named-accounts, kimi-fallback.");
  }
  if ((values["named-account-keys-stdin"] || values["no-key-check"]) && !steps.includes("named-accounts")) {
    throw new RouteError("--named-account-keys-stdin and --no-key-check require the named-accounts step.");
  }
  if (values["named-account-keys-stdin"] && values["no-key-check"]) {
    throw new RouteError("Pass either --named-account-keys-stdin or --no-key-check, not both.");
  }
  return { mode, values, steps };
}

function profile(settings, id) {
  const instance = settings.providerInstances?.[id];
  const driver = id.startsWith("claude_") ? "claudeAgent" : "codex";
  if (instance?.driver !== driver) throw new RouteError(`Refusing ${id}: expected a ${driver} profile.`);
  return instance;
}

function variable(instance, id, name) {
  const matches = (instance.environment ?? []).filter((entry) => entry.name === name);
  if (matches.length > 1) throw new RouteError(`Refusing ${id}: duplicate ${name} entries.`);
  return matches[0];
}

function publicVariable(instance, id, name) {
  const entry = variable(instance, id, name);
  if (entry && (entry.sensitive || entry.valueRedacted || typeof entry.value !== "string")) {
    throw new RouteError(`Refusing ${id}: ${name} is not a plain non-sensitive value.`);
  }
  return entry;
}

function accountProfile(settings, id) {
  const instance = profile(settings, id);
  const prefix = publicVariable(instance, id, "CLIPROXY_ACCOUNT_PREFIX")?.value;
  const route = publicVariable(instance, id, "CLIPROXY_CLAUDE_ROUTE")?.value;
  const fallback = variable(instance, id, "CLIPROXY_FALLBACK_URL");
  if (prefix !== accountPrefixes[id] || (route !== undefined && route !== "account")
      || (fallback && (fallback.valueRedacted || fallback.value !== ""))
      || !publicVariable(instance, id, "CLIPROXY_BASE_URL")) {
    throw new RouteError(`Refusing ${id}: expected its pinned account, an absent or account route, a base URL and no fallback.`);
  }
  return instance;
}

function checkCodexTemplate(settings) {
  const id = "codex_cliproxy";
  const instance = profile(settings, id);
  if (instance.config?.binaryPath !== codexLauncher || Object.entries(fallbackEnv)
    .some(([name, expected]) => publicVariable(instance, id, name)?.value !== expected)) {
    throw new RouteError(`Refusing kimi-fallback: ${id} does not have the expected fallback launcher and environment.`);
  }
}

// Decode T3's launch argument quoting while retaining the original token spans.
function launchTokens(input) {
  const tokens = [];
  for (let index = 0; index < input.length;) {
    if (/\s/.test(input[index])) { index++; continue; }
    const start = index;
    let quote;
    let value = "";
    for (; index < input.length; index++) {
      const char = input[index];
      const next = input[index + 1];
      if (!quote && /\s/.test(char)) break;
      if (char === quote) quote = undefined;
      else if (!quote && (char === '"' || char === "'")) quote = char;
      else if (char === "\\" && next && (quote === '"' ? ['"', "\\", "$", "`"].includes(next) : !quote && /\s/.test(next))) {
        value += next;
        index++;
      } else value += char;
    }
    if (quote) throw new RouteError("Refusing codex_kimi_mac: unclosed quote in launchArgs.");
    tokens.push({ value, start, end: index });
  }
  return tokens;
}

function launchBaseURL(config) {
  const input = config?.launchArgs;
  if (typeof input !== "string") throw new RouteError("Refusing codex_kimi_mac: launchArgs is missing.");
  const tokens = launchTokens(input);
  let setting;
  for (let index = 0; index < tokens.length; index++) {
    let token = tokens[index];
    if (token.value === "--") break;
    let assignment;
    if (["-c", "--config"].includes(token.value)) {
      token = tokens[++index];
      if (!token || token.value.startsWith("-")) throw new RouteError("Refusing codex_kimi_mac: missing config override value.");
      assignment = token.value;
    } else {
      assignment = /^(?:--config=|-c=?)(.+)$/s.exec(token.value)?.[1];
    }
    if (assignment === undefined) continue;

    const equals = assignment.indexOf("=");
    const key = (equals < 0 ? assignment : assignment.slice(0, equals))
      .replace(/\\(?:u([\da-f]{4})|U([\da-f]{8}))/gi, (escape, short, long) => {
        const code = Number.parseInt(short ?? long, 16);
        return code < 128 ? String.fromCharCode(code) : escape;
      })
      .replace(/["'\s]/g, "");
    if (key !== baseURLKey && key !== "model_providers.cliproxy" && key !== "model_providers") continue;
    const match = /^\s*model_providers\.cliproxy\.base_url\s*=\s*(["'])([^"'\\\s]+)\1\s*$/.exec(assignment);
    const raw = input.slice(token.start, token.end);
    const url = match?.[2];
    const offset = url && raw.indexOf(url);
    if (setting || !url || offset < 0 || raw.indexOf(url, offset + url.length) >= 0) {
      throw new RouteError("Refusing codex_kimi_mac: launchArgs must have one unambiguous, quoted CLIProxy base_url override.");
    }
    setting = { value: url, start: token.start + offset, end: token.start + offset + url.length };
  }
  if (!setting) throw new RouteError("Refusing codex_kimi_mac: launchArgs is missing the CLIProxy base_url override.");
  return {
    value: setting.value,
    set: (value) => { config.launchArgs = input.slice(0, setting.start) + value + input.slice(setting.end); },
  };
}

function checkWillOrigin(settings, step, willOrigin) {
  const codex = profile(settings, "codex_cliproxy");
  let codexURL;
  try {
    codexURL = new URL(launchBaseURL(codex.config).value);
  } catch (error) {
    if (!(error instanceof RouteError || error instanceof TypeError)) throw error;
    throw new RouteError(`Refusing ${step}: codex_cliproxy launchArgs must have one valid CLIProxy base_url override.`);
  }
  if (!["http:", "https:"].includes(codexURL.protocol) || codexURL.origin !== willOrigin) {
    throw new RouteError(`Refusing ${step}: --will-origin must match the CLIProxy base_url origin of codex_cliproxy.`);
  }
  const claude = profile(settings, "claude_cliproxy");
  if (publicVariable(claude, "claude_cliproxy", "CLIPROXY_BASE_URL")?.value !== willOrigin) {
    throw new RouteError(`Refusing ${step}: --will-origin must match CLIPROXY_BASE_URL of claude_cliproxy.`);
  }
}

function property(target, name) {
  return { value: target[name], set: (value) => { target[name] = value; } };
}

function locate(settings, { id, kind, name }) {
  if (kind === "default") {
    const selection = settings.defaultModelSelection;
    if (typeof selection?.model !== "string" || !selection.model) {
      throw new RouteError("Refusing default: T3 must return a populated settings.defaultModelSelection.");
    }
    return property(selection, name);
  }
  if (kind === "selection") {
    const selection = settings[name];
    if (typeof selection?.model !== "string" || !selection.model) {
      throw new RouteError(`Refusing ${name}: T3 must return a populated settings.${name}.`);
    }
    return property(selection, "instanceId");
  }
  const instance = profile(settings, id);
  if (kind === "profile") return property(instance, name);
  if (kind === "launchArgs") return launchBaseURL(instance.config);
  if (kind === "config") {
    if (!instance.config) throw new RouteError(`Refusing ${id}: profile config is missing.`);
    return property(instance.config, name);
  }
  const entry = publicVariable(instance, id, name);
  return {
    value: entry?.value ?? null,
    set(value) {
      if (value === null) instance.environment.splice(instance.environment.indexOf(entry), 1);
      else if (entry) entry.value = value;
      else (instance.environment ??= []).push({ name, value, sensitive: false });
    },
  };
}

function fieldName({ id, kind, name }) {
  if (kind === "default") return "defaultModelSelection.instanceId";
  if (kind === "selection") return `${name}.instanceId`;
  const prefix = kind === "profile" ? "" : kind === "launchArgs" ? "config.launchArgs." : `${kind}.`;
  return `${id}.${prefix}${name}`;
}

// Top-level selections that plan steps change, so macReferences does not list them twice.
const planSelections = new Set(["defaultModelSelection", "reviewGuideModelSelection"]);

function macReferences(settings) {
  const references = [];
  function collect(container, prefix) {
    for (const [key, selection] of Object.entries(container ?? {})) {
      if (!key.endsWith("ModelSelection") || (!prefix && planSelections.has(key))) continue;
      const id = selection?.instanceId;
      if (id === "codex_cliproxy_mac" || id === "claude_cliproxy_mac") {
        references.push({ path: `${prefix}${key}`, instanceId: id });
      }
    }
  }
  collect(settings, "");
  for (const [project, overrides] of Object.entries(settings.projectSettingsOverrides ?? {})) {
    collect(overrides, `projectSettingsOverrides[${JSON.stringify(project)}].`);
  }
  return references;
}

function prepareStep(settings, step, fields, { rollback, willOrigin }) {
  if (!rollback && (step === "named-accounts" || step === "kimi-fallback")) checkWillOrigin(settings, step, willOrigin);
  if (step === "named-accounts") {
    for (const id of new Set(fields.map(({ id }) => id))) accountProfile(settings, id);
  }
  if (step === "kimi-fallback") {
    if (!rollback) checkCodexTemplate(settings);
    const kimi = profile(settings, "codex_kimi_mac");
    const override = publicVariable(kimi, "codex_kimi_mac", "T3CODE_CODEX_LAUNCH_ARGS");
    if (override?.value.trim()) throw new RouteError("Refusing codex_kimi_mac: T3CODE_CODEX_LAUNCH_ARGS overrides config.launchArgs.");
  }
  return fields.map((edit) => {
    const label = fieldName(edit);
    const from = rollback ? edit.to : edit.from;
    const to = rollback ? edit.from : edit.to;
    const { value } = locate(settings, edit);
    if (value !== from && value !== to) {
      // A label renamed by hand since apply must not block an urgent route rollback; keep it and report it.
      if (rollback && edit.kind === "profile" && edit.name === "displayName") {
        return { edit, label, from, to, needed: false, skipped: "label changed since apply; left as is" };
      }
      throw new RouteError(`Refusing ${label}: unexpected value. Expected the starting value or target.`);
    }
    return { edit, label, from, to, needed: value !== to };
  });
}

function keysFromStdin() {
  try {
    if (!process.stdin.isTTY) return JSON.parse(fs.readFileSync(0, "utf8"));
  } catch {
    // Never print input or parse diagnostics.
  }
  throw new RouteError("Pass a JSON object of profile IDs to keys on stdin.");
}

async function checkNamedKeys(settings, willOrigin, supplied, fetchModels) {
  return Promise.all(Object.entries(accountPrefixes).map(async ([id, prefix]) => {
    const instance = accountProfile(settings, id);
    const stored = variable(instance, id, "ANTHROPIC_API_KEY");
    const key = stored?.valueRedacted ? supplied?.[id] : stored?.value;
    if (typeof key !== "string" || !/^[A-Za-z0-9._~+/=-]+$/.test(key)) {
      return { id, prefix, status: null, prefixPresent: null,
        error: "No usable ANTHROPIC_API_KEY. For a redacted key, use --named-account-keys-stdin with the existing key for this profile." };
    }
    let status = null;
    let prefixPresent = null;
    try {
      const response = await fetchModels(`${willOrigin}/v1/models`, {
        method: "GET", headers: { authorization: `Bearer ${key}` },
        redirect: "manual", signal: AbortSignal.timeout(10_000),
      });
      status = response.status;
      if (status === 200) {
        const { data } = await response.json();
        if (Array.isArray(data)) prefixPresent = data.some((model) => typeof model?.id === "string" && model.id.startsWith(`${prefix}/`));
      } else await response.body?.cancel();
    } catch {
      // Report status and prefix availability without printing the response or request.
    }
    return { id, prefix, status, prefixPresent };
  }));
}

function writePrivate(file, value) {
  fs.writeFileSync(file, JSON.stringify(value, null, 2) + "\n", { mode: 0o600, flag: "wx" });
}

function readRecord(file) {
  try {
    const record = JSON.parse(fs.readFileSync(file, "utf8"));
    if (![1, 2].includes(record.version) || !Array.isArray(record.changes) || record.changes.length === 0) throw new Error();
    const plan = routePlan(record.willOrigin === undefined ? undefined : willHTTPOrigin(record.willOrigin));
    if (record.version === 1) {
      // Older records stored the route labels in the names step.
      for (const step of ["named-accounts", "kimi-fallback"]) {
        plan.names.push(...plan[step].filter(({ kind }) => kind === "profile"));
        plan[step] = plan[step].filter(({ kind }) => kind !== "profile");
      }
    }
    const seen = new Set();
    const steps = {};
    for (const entry of record.changes) {
      const { step, field: label, from, to } = entry;
      const edit = Object.hasOwn(plan, step) && plan[step].find((candidate) => fieldName(candidate) === label);
      if (!edit || from !== edit.from || to !== edit.to || seen.has(label)) throw new Error();
      seen.add(label);
      (steps[step] ??= []).push(edit);
    }
    if (record.kimiEnvironmentWasAbsent !== undefined &&
        (record.kimiEnvironmentWasAbsent !== true || !steps["kimi-fallback"]?.some(({ kind }) => kind === "environment"))) {
      throw new Error();
    }
    return { steps, kimiEnvironmentWasAbsent: record.kimiEnvironmentWasAbsent };
  } catch {
    throw new RouteError("Invalid rollback record. Use the private record produced by apply.");
  }
}

export async function runCli(args, {
  connect = connectT3, fetchModels = globalThis.fetch, readKeys = keysFromStdin, print = console.log,
} = {}) {
  const report = { ok: false };
  let client;
  try {
    const { mode, values, steps } = options(args);
    report.mode = mode;
    const rollback = mode === "rollback";
    const willOrigin = rollback ? undefined : willHTTPOrigin(values["will-origin"]);
    const record = rollback ? readRecord(values.record) : undefined;
    const available = record?.steps ?? routePlan(willOrigin);
    const selected = (steps ?? Object.keys(available)).map((step) => {
      if (!Object.hasOwn(available, step)) throw new RouteError(`Rollback record has no changes for step ${step}.`);
      return [step, available[step]];
    });
    client = await connect(values["base-dir"], mode === "plan" ? ["orchestration:read"] : undefined);
    const read = async () => (await client.rpc("server.getConfig")).settings;
    const before = await read();
    if (!rollback) report.macReferences = macReferences(before);
    const changes = [];
    report.steps = {};
    for (const [step, fields] of selected) {
      try {
        const prepared = prepareStep(before, step, fields, { rollback, willOrigin });
        report.steps[step] = prepared.map(({ label, from, to, needed, skipped }) => ({
          field: label, from, to, needed, ...(skipped ? { skipped } : {}),
        }));
        changes.push(...prepared.filter(({ needed }) => needed).map((change) => ({ step, ...change })));
      } catch (error) {
        if (!(error instanceof RouteError)) throw error;
        report.steps[step] = { error: error.message };
      }
    }
    if (Object.values(report.steps).some((step) => step.error)) throw new RouteError("One or more selected steps refused the current settings.");
    if (mode !== "plan" && changes.length) {
      if (!rollback && changes.some(({ step }) => step === "named-accounts") && values["no-key-check"]) {
        // T3 stores these keys redacted, so the caller verifies them with a new session on each profile after apply.
        report.keyChecks = "skipped";
      } else if (!rollback && changes.some(({ step }) => step === "named-accounts")) {
        const keys = values["named-account-keys-stdin"] ? readKeys() : {};
        report.keyChecks = await checkNamedKeys(before, willOrigin, keys, fetchModels);
        if (report.keyChecks.some(({ status }) => status !== 200)) {
          throw new RouteError("Every named-account key must receive HTTP 200 from GET /v1/models on Will.");
        }
        if (report.keyChecks.some(({ prefixPresent }) => prefixPresent !== true)) {
          throw new RouteError("Every named account must expose models under its expected prefix in Will's GET /v1/models response.");
        }
      }

      const next = structuredClone(before);
      for (const { edit, to } of changes) locate(next, edit).set(to);
      const changedKimiEnvironment = changes.some(({ edit }) => edit.id === "codex_kimi_mac" && edit.kind === "environment");
      if (rollback && record.kimiEnvironmentWasAbsent && changedKimiEnvironment
          && next.providerInstances.codex_kimi_mac.environment?.length === 0) {
        delete next.providerInstances.codex_kimi_mac.environment;
      }
      const patch = {};
      // T3's server patch replaces the full selection and the full provider map.
      if (changes.some(({ edit }) => edit.kind === "default")) patch.defaultModelSelection = next.defaultModelSelection;
      for (const { edit } of changes) if (edit.kind === "selection") patch[edit.name] = next[edit.name];
      if (changes.some(({ edit }) => edit.id !== null)) patch.providerInstances = next.providerInstances;
      if (!rollback) {
        const dir = values["backup-dir"];
        fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
        const stamp = randomUUID();
        const backupFile = path.join(dir, `settings-before-${stamp}.json`);
        const recordFile = path.join(dir, `profile-routes-${stamp}.json`);
        writePrivate(backupFile, withoutSecrets(before));
        report.backup = backupFile;
        writePrivate(recordFile, {
          version: 2,
          ...(changes.some(({ step }) => step === "named-accounts" || step === "kimi-fallback") ? { willOrigin } : {}),
          ...(changedKimiEnvironment && !Object.hasOwn(before.providerInstances.codex_kimi_mac, "environment") ? { kimiEnvironmentWasAbsent: true } : {}),
          changes: changes.map(({ step, label, from, to }) => ({ step, field: label, from, to })),
        });
        report.record = recordFile;
      }
      const latest = await read();
      if (!isDeepStrictEqual(latest, before)) throw new RouteError("T3 settings changed during preflight. Run the command again.");
      await client.rpc("server.updateSettings", { patch });
      const after = await read();
      report.mismatches = Object.keys(patch).filter((key) => !isDeepStrictEqual(after[key], patch[key]));
      if (report.mismatches.length) throw new RouteError("T3 settings did not match the requested patch on readback.");
    }
    report.ok = true;
  } catch (error) {
    report.error = error instanceof RouteError ? error.message : "Profile routes operation failed. Check T3 and the backup or record path.";
  } finally {
    try {
      client?.close();
    } catch {
      report.ok = false;
      report.sessionCleanupError = "Could not revoke the temporary T3 session.";
    }
  }
  print(JSON.stringify(report));
  return report.ok ? 0 : 1;
}

if (import.meta.main || (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href)) {
  process.exitCode = await runCli(process.argv.slice(2));
}
