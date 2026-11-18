#!/usr/bin/env node
// Sets or removes host failover on one T3 shared-pool Claude profile through server.updateSettings.
// Every other variable, the profile's config block, and every other profile stay as they are.
import fs from "node:fs";
import path from "node:path";
import tty from "node:tty";
import { parseArgs } from "node:util";
import { pathToFileURL } from "node:url";
import { maskHosts } from "./claude_failover_drill.mjs";
import { connectT3, httpOrigin } from "./configure_claude_pool.mjs";

const usage = `Usage: set_pool_fallback.mjs --base-dir <t3-base-dir> --profile-id <id> --primary <origin> (--fallback <origin> | --no-fallback) --backup-dir <private-dir> [--no-key-check], with the profile's API key on stdin.
Before it opens a T3 session, it checks the key with GET /v1/models on the primary and the fallback and requires HTTP 200. --no-key-check skips that check, for example while an origin is down.`;

const keyCheckTimeoutMs = 10_000;
// Only these variables are printed, so a secret T3 saved without its sensitive flag stays unprinted.
const reportedVariables = ["CLIPROXY_CLAUDE_ROUTE", "CLIPROXY_BASE_URL", "CLIPROXY_FALLBACK_URL"];

// Its message holds no secret values, so main prints it as is.
class PrintableError extends Error {}

function origin(value, name) {
  try {
    return httpOrigin(value, name);
  } catch {
    throw new PrintableError(`The ${name} must be an HTTP origin without credentials or a path.`);
  }
}

function apiKeyValue(value) {
  const key = value?.trim();
  if (!key) throw new PrintableError("The profile's API key is required.");
  if (!/^[A-Za-z0-9._~+/=-]+$/.test(key)) {
    throw new PrintableError("The API key may hold only letters, digits and the characters . _ ~ + / = -, with no whitespace.");
  }
  return key;
}

// Validates everything that needs no T3 session. A null fallback means remove it.
export function fallbackRequest({ primary, fallback, apiKey }) {
  if (fallback === undefined) throw new PrintableError("Pass a fallback origin, or null to remove it.");
  const primaryOrigin = origin(primary, "primary");
  const fallbackOrigin = fallback === null ? null : origin(fallback, "fallback");
  if (fallbackOrigin === primaryOrigin) throw new PrintableError("The fallback must be a different CLIProxy host.");
  return { primary: primaryOrigin, fallback: fallbackOrigin, apiKey: apiKeyValue(apiKey) };
}

// A key the hosts reject would pass the settings check and fail only in the next session.
export async function checkKey({ primary, fallback, apiKey }, timeoutMs = keyCheckTimeoutMs) {
  for (const [name, target] of [["primary", primary], ["fallback", fallback]]) {
    if (!target) continue;
    let status = null;
    try {
      const response = await fetch(`${target}/v1/models`, {
        headers: { authorization: `Bearer ${apiKey}` }, redirect: "manual", signal: AbortSignal.timeout(timeoutMs),
      });
      status = response.status;
      await response.body?.cancel();
    } catch {
      // No response is reported below like a rejected key.
    }
    if (status !== 200) {
      throw new PrintableError(`The key check on the ${name} ${maskHosts(target)} failed: GET /v1/models returned ${status ?? "no response"}, not 200. Check the key, or pass --no-key-check while that origin is down.`);
    }
  }
}

// T3 returns sensitive values redacted, so the API key is passed again to keep it stored.
// A null fallback removes CLIPROXY_FALLBACK_URL.
export function poolFallbackEnvironment(environment, options) {
  const { primary, fallback, apiKey } = fallbackRequest(options);
  const count = (name, value) => environment.filter((v) => v.name === name && (value === undefined || v.value === value)).length;
  if (!count("CLIPROXY_CLAUDE_ROUTE", "shared-pool")) throw new PrintableError("Refusing a profile that is not a shared Claude pool.");
  if (!count("ANTHROPIC_API_KEY")) throw new PrintableError("Refusing a pool profile without ANTHROPIC_API_KEY.");
  if (!count("CLIPROXY_BASE_URL")) throw new PrintableError("Refusing a pool profile without CLIPROXY_BASE_URL.");
  for (const name of ["CLIPROXY_CLAUDE_ROUTE", "ANTHROPIC_API_KEY", "CLIPROXY_BASE_URL", "CLIPROXY_FALLBACK_URL"]) {
    if (count(name) > 1) throw new PrintableError(`Refusing a pool profile with more than one ${name}.`);
  }
  return environment.filter((v) => v.name !== "CLIPROXY_FALLBACK_URL").flatMap((v) => {
    if (v.name === "ANTHROPIC_API_KEY") return [{ name: v.name, value: apiKey, sensitive: true }];
    if (v.name !== "CLIPROXY_BASE_URL") return [v];
    const base = { name: v.name, value: primary, sensitive: false };
    return fallback ? [base, { name: "CLIPROXY_FALLBACK_URL", value: fallback, sensitive: false }] : [base];
  });
}

// Compares the provider instances T3 saved with the edit that was sent. The report holds no secret
// values and no tailnet name; `ok` is false when any check failed.
const credentialName = /KEY|TOKEN|SECRET|PASSWORD/i;

// A copy of T3 settings with every non-empty credential-like variable value replaced.
export function withoutSecrets(settings) {
  const copy = structuredClone(settings);
  for (const instance of Object.values(copy?.providerInstances ?? {})) {
    for (const variable of instance?.environment ?? []) {
      if (credentialName.test(variable.name) && typeof variable.value === "string" && variable.value) variable.value = "<redacted>";
    }
  }
  return copy;
}

export function savedReport({ id, before, after, environment }) {
  const profile = before[id];
  const saved = after[id];
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const visible = (variables) => (variables ?? []).filter((v) => !v.sensitive).map((v) => [v.name, v.value]).sort();
  const otherSecrets = (variables) => (variables ?? []).filter((v) => v.sensitive && v.name !== "ANTHROPIC_API_KEY").map((v) => v.name).sort();
  const key = saved?.environment?.find((v) => v.name === "ANTHROPIC_API_KEY");
  const others = (instances) => Object.keys(instances).filter((name) => name !== id).sort();
  const report = {
    profile: id,
    environment: Object.fromEntries((saved?.environment ?? [])
      .filter((v) => !v.sensitive && reportedVariables.includes(v.name))
      .map((v) => [v.name, maskHosts(v.value)])),
    apiKey: { sensitive: !!key?.sensitive, redacted: !!key?.valueRedacted, valueVisible: !!key?.value },
    environmentApplied: same(visible(saved?.environment), visible(environment)),
    profileUnchanged: saved?.driver === profile.driver && saved?.displayName === profile.displayName && saved?.enabled === profile.enabled &&
      same(otherSecrets(saved?.environment), otherSecrets(profile.environment)),
    configUnchanged: same(saved?.config, profile.config),
    otherProfilesUnchanged: same(others(after), others(before)) && others(before).every((name) => same(after[name], before[name])),
  };
  const { apiKey: stored } = report;
  const ok = stored.sensitive && stored.redacted && !stored.valueVisible && report.environmentApplied &&
    report.profileUnchanged && report.configUnchanged && report.otherProfilesUnchanged;
  return { report, ok };
}

function parseOptions() {
  let values;
  try {
    ({ values } = parseArgs({
      options: {
        "base-dir": { type: "string" }, "profile-id": { type: "string" }, primary: { type: "string" },
        fallback: { type: "string" }, "no-fallback": { type: "boolean" }, "backup-dir": { type: "string" },
        "no-key-check": { type: "boolean" },
      },
    }));
  } catch {
    // parseArgs errors can echo a stray argument, which might be a key.
    throw new PrintableError(usage);
  }
  for (const name of ["base-dir", "profile-id", "primary", "backup-dir"]) {
    if (!values[name]) throw new PrintableError(`--${name} is required. ${usage}`);
  }
  if (!values.fallback === !values["no-fallback"]) throw new PrintableError(`Pass exactly one of --fallback <origin> or --no-fallback. ${usage}`);
  if (!/^[a-zA-Z0-9_-]+$/.test(values["profile-id"])) throw new PrintableError("A valid profile ID is required.");
  return values;
}

async function main() {
  const values = parseOptions();
  const id = values["profile-id"];
  if (tty.isatty(0)) throw new PrintableError("Pipe the profile's API key on stdin.");
  // Arguments, origins and the key are all checked before a T3 session is issued.
  const request = fallbackRequest({
    primary: values.primary, fallback: values["no-fallback"] ? null : values.fallback, apiKey: fs.readFileSync(0, "utf8"),
  });
  if (!values["no-key-check"]) await checkKey(request);
  fs.mkdirSync(values["backup-dir"], { recursive: true, mode: 0o700 });
  const client = await connectT3(values["base-dir"]);
  let failed = false;
  let failure;
  try {
    const { settings } = await client.rpc("server.getConfig");
    const before = settings.providerInstances;
    const profile = before[id];
    if (profile?.driver !== "claudeAgent") throw new PrintableError(`Refusing ${id}: it is not a claudeAgent profile.`);
    const environment = poolFallbackEnvironment(profile.environment ?? [], request);
    const stamp = new Date().toISOString().replace(/[:.]/g, "");
    // Backups never hold a credential value, even one T3 failed to flag or redact.
    const backup = (name, value) => fs.writeFileSync(path.join(values["backup-dir"], `settings-${name}-${stamp}.json`), JSON.stringify(withoutSecrets(value), null, 2), { flag: "wx", mode: 0o600 });
    backup("before", settings);
    await client.rpc("server.updateSettings", { patch: { providerInstances: { ...before, [id]: { ...profile, environment } } } });
    const { settings: after } = await client.rpc("server.getConfig");
    backup("after", after);
    const { report, ok } = savedReport({ id, before, after: after.providerInstances, environment });
    console.log(JSON.stringify(report));
    if (!ok) throw new PrintableError("The saved settings did not verify. Restore the profile from the private before backup.");
  } catch (error) {
    failed = true;
    failure = error;
  }
  // A failed session revoke must not hide what happened to the settings.
  try {
    client.close();
  } catch {
    if (!failed) {
      failed = true;
      failure = new PrintableError("The settings were saved and verified, but the temporary T3 session could not be revoked; it expires within 5 minutes.");
    }
  }
  if (failed) throw failure;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error instanceof PrintableError ? error.message : "Pool fallback change failed. Check the arguments, T3, and the private backup directory; no secret values are printed.");
    process.exitCode = 1;
  });
}
