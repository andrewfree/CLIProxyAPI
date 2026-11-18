#!/usr/bin/env node
// Sets or removes host failover on one T3 shared-pool Claude profile through server.updateSettings.
// Every other variable, the profile's config block, and every other profile stay as they are.
import fs from "node:fs";
import path from "node:path";
import tty from "node:tty";
import { parseArgs } from "node:util";
import { pathToFileURL } from "node:url";
import { connectT3, httpOrigin } from "./configure_claude_pool.mjs";

const usage = "Usage: set_pool_fallback.mjs --base-dir <t3-base-dir> --profile-id <id> --primary <origin> (--fallback <origin> | --no-fallback) --backup-dir <private-dir>, with the profile's API key on stdin.";

// Its message holds no secret values, so main prints it as is.
class PrintableError extends Error {}

function origin(value, name) {
  try {
    return httpOrigin(value, name);
  } catch {
    throw new PrintableError(`The ${name} must be an HTTP origin without credentials or a path.`);
  }
}

// T3 returns sensitive values redacted, so the API key is passed again to keep it stored.
// A null fallback removes CLIPROXY_FALLBACK_URL.
export function poolFallbackEnvironment(environment, { primary, fallback, apiKey }) {
  if (fallback === undefined) throw new PrintableError("Pass a fallback origin, or null to remove it.");
  const primaryOrigin = origin(primary, "primary");
  const fallbackOrigin = fallback === null ? null : origin(fallback, "fallback");
  if (fallbackOrigin === primaryOrigin) throw new PrintableError("The fallback must be a different CLIProxy host.");
  if (!apiKey?.trim()) throw new PrintableError("The profile's API key is required.");
  const has = (name, value) => environment.some((v) => v.name === name && (value === undefined || v.value === value));
  if (!has("CLIPROXY_CLAUDE_ROUTE", "shared-pool")) throw new PrintableError("Refusing a profile that is not a shared Claude pool.");
  if (!has("ANTHROPIC_API_KEY")) throw new PrintableError("Refusing a pool profile without ANTHROPIC_API_KEY.");
  if (!has("CLIPROXY_BASE_URL")) throw new PrintableError("Refusing a pool profile without CLIPROXY_BASE_URL.");
  return environment.filter((v) => v.name !== "CLIPROXY_FALLBACK_URL").flatMap((v) => {
    if (v.name === "ANTHROPIC_API_KEY") return [{ name: v.name, value: apiKey.trim(), sensitive: true }];
    if (v.name !== "CLIPROXY_BASE_URL") return [v];
    const base = { name: v.name, value: primaryOrigin, sensitive: false };
    return fallbackOrigin ? [base, { name: "CLIPROXY_FALLBACK_URL", value: fallbackOrigin, sensitive: false }] : [base];
  });
}

function parseOptions() {
  let values;
  try {
    ({ values } = parseArgs({
      options: {
        "base-dir": { type: "string" }, "profile-id": { type: "string" }, primary: { type: "string" },
        fallback: { type: "string" }, "no-fallback": { type: "boolean" }, "backup-dir": { type: "string" },
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
  const apiKey = fs.readFileSync(0, "utf8");
  const options = { primary: values.primary, fallback: values["no-fallback"] ? null : values.fallback, apiKey };
  fs.mkdirSync(values["backup-dir"], { recursive: true, mode: 0o700 });
  const client = await connectT3(values["base-dir"]);
  try {
    const { settings } = await client.rpc("server.getConfig");
    const before = settings.providerInstances;
    const profile = before[id];
    if (profile?.driver !== "claudeAgent") throw new PrintableError(`Refusing ${id}: it is not a claudeAgent profile.`);
    const environment = poolFallbackEnvironment(profile.environment ?? [], options);
    const stamp = new Date().toISOString().replace(/[:.]/g, "");
    const backup = (name, value) => fs.writeFileSync(path.join(values["backup-dir"], `settings-${name}-${stamp}.json`), JSON.stringify(value, null, 2), { flag: "wx", mode: 0o600 });
    backup("before", settings);
    await client.rpc("server.updateSettings", { patch: { providerInstances: { ...before, [id]: { ...profile, environment } } } });
    const { settings: after } = await client.rpc("server.getConfig");
    backup("after", after);

    const saved = after.providerInstances[id];
    const visible = (variables) => (variables ?? []).filter((v) => !v.sensitive).map((v) => [v.name, v.value]);
    const sameVariables = (a, b) => JSON.stringify(visible(a).sort()) === JSON.stringify(visible(b).sort());
    const key = saved?.environment?.find((v) => v.name === "ANTHROPIC_API_KEY");
    const others = (instances) => Object.keys(instances).filter((name) => name !== id).sort();
    const report = {
      profile: id,
      environment: Object.fromEntries(visible(saved?.environment)),
      apiKey: { sensitive: !!key?.sensitive, redacted: !!key?.valueRedacted, valueVisible: !!key?.value },
      environmentApplied: sameVariables(saved?.environment, environment),
      configUnchanged: JSON.stringify(saved?.config) === JSON.stringify(profile.config),
      otherProfilesUnchanged: JSON.stringify(others(after.providerInstances)) === JSON.stringify(others(before)) &&
        others(before).every((name) => JSON.stringify(after.providerInstances[name]) === JSON.stringify(before[name])),
    };
    console.log(JSON.stringify(report));
    const { apiKey: stored } = report;
    if (!stored.sensitive || !stored.redacted || stored.valueVisible || !report.environmentApplied || !report.configUnchanged || !report.otherProfilesUnchanged) {
      throw new PrintableError("The saved settings did not verify. Restore the profile from the private before backup.");
    }
  } finally {
    client.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error instanceof PrintableError ? error.message : "Pool fallback change failed. Check the arguments, T3, and the private backup directory; no secret values are printed.");
    process.exitCode = 1;
  });
}
