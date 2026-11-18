#!/usr/bin/env python3
"""Read-only CLIProxy fleet probes. Public reports contain no account or key tags."""

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import hmac
from http import HTTPStatus
import http.client
import json
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
from urllib.parse import urlencode, urlsplit

OPS = Path(__file__).resolve().parent
LOADED = "/v0/management/config"
V8 = "/v8/management"
FLEET_HOSTS = ("m5", "will")
RUNTIME_FIELDS = (
    "request-retry", "max-retry-credentials", "max-retry-interval",
    "quota-cooldown-max-seconds", "save-cooldown-status",
    "transient-error-cooldown-seconds", "usage-statistics-enabled",
)
ROUTING_FIELDS = (
    "strategy", "session-affinity", "session-affinity-ttl",
    "session-affinity-subagents", "session-affinity-rebind-on",
)
NVIDIA_MODELS = {
    "nvidia-nemotron-3.5-lightning", "nvidia-minimax-m3.1-flash",
    "nvidia-nemotron-3-ultra",
}
STRICT_SURFACES = {
    "P2/shared-pin", "P2/host-pin", "P6/duplicate-named-prefix",
    "P6/missing-label", "P6/unknown-prefix", "P10/auth-cursor-pin",
}
PARITY_PREFIXES = {"P1", "P2", "P2h", "P3", "P5", "P6", "P7", "P8", "P9", "P10"}
SECRET_KEY = re.compile(
    r"(^|-)(api-?keys?|secrets?|tokens?|passwords?|authorization|"
    r"private-key|cookie)(-|$)", re.IGNORECASE,
)
TOKEN_SETTING = re.compile(r"(?:use-)?(?:max|min)(?:-(?:completion|input|output))?-tokens")
STATIC_KEY_FAMILIES = (
    "openai-compatibility", "claude-api-key", "codex-api-key", "gemini-api-key",
    "interactions-api-key", "vertex-api-key", "xai-api-key", "meta-api-key",
)
HARD_CREDENTIAL_ERROR = re.compile(r"\b(?:unauthorized|payment required|invalid grant|token (?:has )?expired)\b")
HARD_CREDENTIAL_REASONS = {"unauthorized", "payment_required", "invalid_grant"}
COOLDOWN_REASONS = {"quota", "credential_quota", "transient_rate_limit"}
COOLDOWN_MESSAGES = {
    "quota", "credential quota", "quota exhausted", "rate limit",
    "rate limit exceeded", "transient rate limit", "too many requests",
}
HTTP_STATUS_ERRORS = {status.value: f"Read-only API returned HTTP {status.value}"
                      for status in HTTPStatus}


class ProbeError(Exception):
    """A rejected API response or unusable input with a public, fixed message."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def tag(nonce, *value):
    return hmac.new(nonce, canonical(value), hashlib.sha256).hexdigest()[:12]


def at_path(value, path, default=None):
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def provider_list(value):
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def redact(value, secret_paths, hidden, path=()):
    """Keep functional config while retaining secrets only for in-process tagging."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            item_path = (*path, key)
            folded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", key)
            folded = re.sub(r"[^a-zA-Z0-9]+", "-", folded).lower()
            secret_name = ((SECRET_KEY.search(folded) and not TOKEN_SETTING.fullmatch(folded))
                           or folded in ("credential", "credentials"))
            if key in STATIC_KEY_FAMILIES:
                secret_name = bool(path) or not provider_list(item)
            if ".".join(item_path) in secret_paths or secret_name:
                hidden.append((item_path, item))
            elif folded == "headers":
                if isinstance(item, dict):
                    hidden.extend(((*item_path, name), val) for name, val in item.items())
                    result[key] = {}
                else:
                    hidden.append((item_path, item))
            else:
                result[key] = redact(item, secret_paths, hidden, item_path)
        return result
    if isinstance(value, list):
        return [redact(item, secret_paths, hidden, (*path, str(i)))
                for i, item in enumerate(value)]
    if isinstance(value, str) and "://" in value:
        try:
            url = urlsplit(value)
            secret_url = url.netloc and (url.username or url.password or url.query or url.fragment)
        except ValueError:
            secret_url = True
        if secret_url:
            hidden.append((path, value))
            return None
    return value


def split_config(config):
    """Isolate the specific differences recorded in the initial fleet baseline."""
    shared = dict(config)
    debug = shared.pop("debug", None)
    providers = shared.get("openai-compatibility")
    nvidia = []
    if providers is None:
        shared["openai-compatibility"] = []
    elif provider_list(providers):
        nvidia = [item for item in providers if item.get("name") == "nvidia"]
        shared["openai-compatibility"] = [item for item in providers if item.get("name") != "nvidia"]
    plugins = dict(shared.get("plugins") or {})
    configs = dict(plugins.get("configs") or {})
    quota_plugin = configs.pop("cliproxybar-quota", None)
    plugins["configs"] = configs
    shared["plugins"] = plugins
    return {"shared": shared, "debug": debug, "nvidia": nvidia, "cliproxybar-quota": quota_plugin}


def config_snapshot(config, paths, nonce):
    config = {key: value for key, value in config.items() if key not in paths["host_paths"]}
    view = redact(config, paths["secret_paths"], [])
    digests = {}
    private = {}
    scope_paths = {
        "shared": (),
        "debug": ("debug",),
        "nvidia": ("openai-compatibility",),
        "cliproxybar-quota": ("plugins", "configs", "cliproxybar-quota"),
    }
    for scope, value in split_config(config).items():
        hidden = []
        scoped = redact(value, paths["secret_paths"], hidden, scope_paths[scope])
        digests[scope] = fingerprint(scoped)
        # The top-level client keys are compared as a set and drive /v1/models.
        private[scope] = tag(nonce, "provider-config", sorted(
            item for item in hidden if item[0] != ("api-keys",)
        ))
    public = {
        "config_sha256": fingerprint(view),
        "shared_config_sha256": digests["shared"],
        "section_sha256": {key: fingerprint(view[key]) for key in paths["fingerprint_keys"]
                           if key in view},
        "values": {},
    }
    for path, allowed in paths["print_paths"].items():
        value = at_path(view, path)
        if any(canonical(value) == canonical(candidate) for candidate in allowed):
            public["values"][path] = value
    return digests, private, public


class LocalAPI:
    def __init__(self, port, env_file):
        self.port = port
        self.env_file = env_file
        self._password = None

    def management_key(self):
        if self._password is None:
            values = []
            for line in Path(self.env_file).read_text(encoding="utf-8").splitlines():
                name, _, value = line.strip().partition("=")
                if name.strip() in ("MANAGEMENT_PASSWORD", "export MANAGEMENT_PASSWORD"):
                    values.extend(shlex.split(value, comments=True))
            if len(values) != 1 or not values[0]:
                raise ProbeError("Management key missing or ambiguous")
            self._password = values[0]
        return self._password

    def get(self, path, *, client_key=None, optional_status=None):
        headers = {"Accept": "application/json"}
        if path.startswith(("/v0/management/", "/v8/management/")):
            headers["Authorization"] = "Bearer " + self.management_key()
        elif client_key is not None:
            headers["Authorization"] = "Bearer " + client_key
        # Loopback only. HTTPConnection follows neither proxies nor redirects.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if response.status == optional_status:
                return None, None
            if response.status != 200:
                raise ProbeError(HTTP_STATUS_ERRORS.get(
                    response.status, "Read-only API returned an unexpected HTTP status"
                ))
            return json.load(response), response.getheader("X-CPA-Version")
        except (OSError, http.client.HTTPException):
            raise ProbeError("Loopback API unavailable") from None
        finally:
            connection.close()


def registered_prefix(ids, forced):
    if not ids:
        return None
    pairs = {prefix for model in ids for prefix, slash, base in [model.partition("/")]
             if slash and base in ids}
    if len(pairs) == 1:
        return next(iter(pairs))
    if pairs:
        return None
    if forced:
        prefixes = {model.partition("/")[0] for model in ids if "/" in model}
        if len(prefixes) == 1 and all("/" in model for model in ids):
            return next(iter(prefixes))
        if prefixes:
            return None
    return ""


def credential_health(item, now):
    if item["disabled"] or item["status"] == "disabled":
        return "disabled"
    message = re.sub(r"[_-]+", " ", (item.get("status_message") or "").strip().lower())
    if item.get("requires_sign_in") or HARD_CREDENTIAL_ERROR.search(message):
        return "error"
    cooldowns = item.get("cooldowns") or []
    if any(row["reason"] in HARD_CREDENTIAL_REASONS for row in cooldowns):
        return "error"
    # Restored model restrictions can set the aggregate error status.
    if cooldowns and all(row["reason"] == "model_not_supported" for row in cooldowns):
        return "ok"
    retry = item.get("next_retry_after")
    if item["unavailable"] and retry:
        deadline = datetime.fromisoformat(retry.replace("Z", "+00:00"))
        # Passive quota observations do not establish which failure set this timer.
        if deadline > now and (message in COOLDOWN_MESSAGES or any(
            row["reason"] in COOLDOWN_REASONS
            and datetime.fromisoformat(row["retry_at"].replace("Z", "+00:00")) == deadline
            for row in cooldowns
        )):
            return "cooling"
    if item["unavailable"] or item["status"] == "error":
        return "error"
    return "ok"


def credential_snapshot(api, config, nonce, now):
    records = []
    direct_ids = set()
    named = Counter()
    counts = Counter()
    issues = Counter()
    catalog_scopes = {}
    response, _ = api.get(V8 + "/credentials")
    for item in response["files"]:
        provider = item["provider"]
        disabled = item["disabled"] or item["status"] == "disabled"
        health = credential_health(item, now)
        counts[health] += 1
        label = item["label"]
        if not label:
            issues["P6/missing-label"] += 1
        account = tag(nonce, "account", provider, label)
        prefix = None
        if not disabled:
            # ID is accepted by the handler and avoids ambiguity from duplicate filenames.
            query = urlencode({"name": item["id"]})
            models, _ = api.get(V8 + "/credentials/models?" + query)
            ids = {model["id"] for model in models["models"]}
            prefix = registered_prefix(ids, config["force-model-prefix"])
            if prefix is None:
                issues["P6/unknown-prefix"] += 1
            if provider != "cursor":
                direct_ids.update(tag(nonce, "model", model) for model in ids)
            if prefix and prefix.startswith("t3-"):
                named[provider, prefix] += 1
            for model in models["models"]:
                if provider == "kimi-ai":
                    catalog_scopes[model["id"], model.get("owned_by")] = "kimi-ai"
                elif provider == "codex" and prefix == "camoff-astra" and model["id"].startswith("camoff-astra/"):
                    catalog_scopes[model["id"], model.get("owned_by")] = "camoff-astra"
        scope = "kimi-ai" if provider == "kimi-ai" else "shared"
        is_camoff = provider == "codex" and prefix == "camoff-astra"
        prefix_kind = "camoff-astra" if is_camoff else "unprefixed" if prefix == "" else "other"
        records.append([scope, account, tag(nonce, "prefix", prefix), disabled,
                        None if disabled else item.get("priority", 0), prefix_kind, health])
    issues["P6/duplicate-named-prefix"] = sum(count > 1 for count in named.values())
    return (records, direct_ids, catalog_scopes, dict(counts),
            {key: val for key, val in issues.items() if val})


def catalog_snapshot(api, keys, nonce, catalog_scopes):
    if not isinstance(keys, list) or not keys or any(not isinstance(k, str) or not k for k in keys):
        raise ProbeError("Loaded config has no usable client key set")
    catalogs = {}
    for key in sorted(set(keys)):
        client = tag(nonce, "client-key", key)
        response, _ = api.get("/v1/models", client_key=key)
        models = {}
        for row in response["data"]:
            model, owner = row["id"], row["owned_by"]
            scope = catalog_scopes.get((model, owner), "shared")
            if model in NVIDIA_MODELS and owner == "nvidia":
                scope = "nvidia"
            model_id = tag(nonce, "model", model)
            value = [tag(nonce, "owner", owner), scope]
            if model_id in models and models[model_id] != value:
                raise ProbeError("Catalog reported conflicting owners for one ID")
            models[model_id] = value
        catalogs[client] = models
    return catalogs


def static_direct_ids(config, nonce):
    # Config-backed keys are absent from the credential-file list. Check their
    # declared routes as well, including a base route when prefixes are optional.
    ids = set()
    for family in STATIC_KEY_FAMILIES:
        for provider in config.get(family) or []:
            if provider.get("disabled"):
                continue
            prefix = provider.get("prefix", "").strip()
            for model in provider.get("models") or []:
                model_id = (model.get("alias") or model["name"]).strip()
                if not prefix or not config["force-model-prefix"] or prefix == model_id:
                    ids.add(tag(nonce, "model", model_id))
                if prefix:
                    ids.add(tag(nonce, "model", prefix + "/" + model_id))
    return ids


def hidden_snapshot(api, host, paths, nonce):
    values = {}
    issues = {}
    for path, default in paths["hidden_paths"].items():
        observed, _ = api.get(V8 + "/config/" + path.replace(".", "/"), optional_status=404)
        value = default if observed is None else observed
        if (path == "server.response-compression.min-bytes" and type(value) is int
                and value <= 0):
            value = default
        if path in host["hidden_expected"]:
            if canonical(value) not in [canonical(x) for x in host["hidden_expected"][path]]:
                issues["P2h/" + path] = 1
        else:
            values[path] = tag(nonce, "hidden-config", path, value)
    return values, issues


def plugin_snapshot(api, host, release, nonce):
    response, _ = api.get(V8 + "/plugins")
    entries = []
    cursor_ok = False
    for item in response["plugins"]:
        plugin = item["id"]
        version = (item["metadata"] or {}).get("version")
        enabled = item["effective_enabled"]
        scope = "cliproxybar-quota" if (plugin, version, enabled) == (
            "cliproxybar-quota", "0.1.0", True
        ) else "shared"
        entries.append([scope, tag(nonce, "plugin", plugin, version, enabled)])
        if plugin == "auth-cursor":
            cursor_ok = item["registered"] and enabled and version == release["cursor_plugin"]["version"]
    library = release["cursor_plugin"]["libraries"][host["arch"]]
    filename = Path(host["instance_root"]) / "plugins/linux" / host["arch"] / library["filename"]
    try:
        library_ok = hashlib.sha256(filename.read_bytes()).hexdigest() == library["sha256"]
    except OSError:
        library_ok = False
    return entries, cursor_ok and library_ok and response["plugins_enabled"]


def check_live_policy(config, host, release):
    expected = dict(release["runtime_policy"], **{"usage-statistics-enabled": False})
    expected.update({"routing." + key: val
                     for key, val in release["routing_policy"][host["arch"]].items()})
    return {"P3/" + path: 1 for path, value in expected.items()
            if canonical(at_path(config, path)) != canonical(value)}


def collect(host, paths, release, nonce, command, api=None):
    if api is None:
        api = LocalAPI(host["port"], Path(host["instance_root"]) / ".env")
    try:
        health, _ = api.get("/healthz")
        if health != {"status": "ok"}:
            raise ProbeError("Unexpected health response")
    except (ProbeError, ValueError):
        return {"status": "unreachable", "error": "Health probe failed"}
    if command == "health":
        return {"status": "ok"}

    config, version = api.get(LOADED)
    digests, private, public = config_snapshot(config, paths, nonce)
    accounts, direct, catalog_scopes, health_counts, issues = credential_snapshot(
        api, config, nonce, datetime.now(timezone.utc)
    )
    direct.update(static_direct_ids(config, nonce))
    catalogs = catalog_snapshot(api, config["api-keys"], nonce, catalog_scopes)
    hidden, hidden_issues = hidden_snapshot(api, host, paths, nonce)
    plugins, cursor_ok = plugin_snapshot(api, host, release, nonce)
    cursor, _ = api.get(V8 + "/routing/model-definitions/cursor")
    cursor_ids = {tag(nonce, "model", row["id"]) for row in cursor["models"]}
    glm_52 = tag(nonce, "model", "glm-5.2")
    issues.update(hidden_issues)
    issues.update(check_live_policy(config, host, release))
    if version != release["version"]:
        issues["P1/version"] = 1
    if not cursor_ok:
        issues["P10/auth-cursor-pin"] = 1
    if digests["shared"] != release["config_fingerprint"]["shared"]:
        issues["P2/shared-pin"] = 1
    observed_pin = release["config_fingerprint"]["observed"].get(host["name"])
    if observed_pin and public["config_sha256"] != observed_pin:
        issues["P2/host-pin"] = 1
    public.update({
        "credential_health": health_counts,
        "camoff_astra_credentials": sum(row[5] == "camoff-astra" for row in accounts),
        "client_keys": len(catalogs),
        "catalog_sizes": sorted(map(len, catalogs.values())),
        "plugins": len(plugins),
    })
    return {
        "status": "ok", "public": public, "issues": issues, "config": digests,
        "private_config": private, "accounts": accounts, "catalogs": catalogs, "hidden": hidden,
        "plugins": plugins, "direct": sorted(direct - {glm_52}),
        "cursor": sorted(cursor_ids - {glm_52}),
        "glm_5.2_direct": glm_52 in direct, "glm_5.2_cursor": glm_52 in cursor_ids,
    }


def safe_collect(host, paths, release, nonce, command):
    try:
        return collect(host, paths, release, nonce, command)
    except ProbeError as error:
        return {"status": "error", "error": str(error)}
    except Exception:
        # Parse errors may include URLs, file names, credentials or response bodies.
        return {"status": "error", "error": "Could not read fleet probe inputs"}


def valid_ssh_host(name, destination):
    if name == "m5":
        return destination is None
    return (name == "will" and isinstance(destination, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", destination) is not None)


def probe(name, hosts, paths, release, nonce, command):
    host = hosts[name]
    destination = host["ssh"]
    if not valid_ssh_host(name, destination):
        return {"status": "error", "error": "Invalid SSH host"}
    if destination is None:
        return safe_collect(host, paths, release, nonce, command)

    # Run source from this checkout over SSH stdin. Nothing is installed on Will.
    payload = (host, paths, release, nonce, command)
    program = Path(__file__).read_text(encoding="utf-8")
    program += "\nprint(json.dumps(safe_collect(*" + repr(payload) + ")))\n"
    try:
        result = subprocess.run(
            ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
             "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
             "--", destination, "python3", "-I", "-B", "-", "--worker"],
            input=program, capture_output=True, text=True, timeout=90, check=False,
        )
        if result.returncode == 255:
            return {"status": "unreachable", "error": "SSH probe failed"}
        response = json.loads(result.stdout)
        if result.returncode == 0 and isinstance(response, dict):
            return response
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return {"status": "error", "error": "Could not complete SSH probe"}


def compare(m5, will):
    differences = []

    def add(surface, hosts, count=1):
        if count:
            differences.append((surface, hosts, count))

    for host, snapshot in zip(FLEET_HOSTS, (m5, will)):
        for surface, count in snapshot["issues"].items():
            add(surface, (host,), count)

    for scope, digest in m5["config"].items():
        if digest != will["config"][scope]:
            hosts = {"nvidia": ("m5",), "cliproxybar-quota": ("will",)}.get(scope, FLEET_HOSTS)
            # An exception for an added item cannot hide differing definitions on both hosts.
            empty = {"nvidia": [], "cliproxybar-quota": None}.get(scope)
            if scope in ("nvidia", "cliproxybar-quota"):
                hosts = tuple(host for host, snapshot in zip(FLEET_HOSTS, (m5, will))
                              if snapshot["config"][scope] != fingerprint(empty))
            add("P2/" + scope, hosts)
    for scope, digest in m5["private_config"].items():
        if digest != will["private_config"][scope]:
            hosts = FLEET_HOSTS
            if scope == "nvidia":
                hosts = tuple(host for host, snapshot in zip(FLEET_HOSTS, (m5, will))
                              if snapshot["config"][scope] != fingerprint([]))
            add("P5/" + scope, hosts)
    for path, digest in m5["hidden"].items():
        if digest != will["hidden"][path]:
            add("P2h/" + path, FLEET_HOSTS)

    left_keys, right_keys = set(m5["catalogs"]), set(will["catalogs"])
    add("P5/client-keys", FLEET_HOSTS, len(left_keys ^ right_keys))
    for key in left_keys & right_keys:
        left, right = m5["catalogs"][key], will["catalogs"][key]
        for host, observed, peer in (("m5", left, right), ("will", right, left)):
            counts = Counter(observed[model][1] for model in observed.keys() - peer.keys())
            for scope, count in counts.items():
                add("P8/" + scope, (host,), count)
        add("P8/owner", FLEET_HOSTS, sum(left[model][0] != right[model][0]
                                        for model in left.keys() & right.keys()))

    left = Counter(tuple(row[:-1]) for row in m5["accounts"])
    right = Counter(tuple(row[:-1]) for row in will["accounts"])
    # Permit the one observed prefix change only if the account, enabled flag and priority match.
    m5_only, will_only = left - right, right - left
    for row in tuple(m5_only):
        if row[-1] != "camoff-astra":
            continue
        same = next((peer for peer in will_only if peer[:2] == row[:2]
                     and peer[3:5] == row[3:5] and peer[-1] == "unprefixed"), None)
        if same is not None:
            count = min(m5_only[row], will_only[same])
            add("P6/camoff-astra", ("m5",), count)
            m5_only[row] -= count
            will_only[same] -= count
    for host, extra in (("m5", m5_only), ("will", will_only)):
        counts = Counter()
        for row, count in extra.items():
            counts[row[0]] += count
        for scope, count in counts.items():
            add("P6/" + scope, (host,), count)
    for host, observed, peer in (("m5", m5, will), ("will", will, m5)):
        present = Counter(row[1] for row in peer["accounts"] if not row[3])
        errors = Counter(row[1] for row in observed["accounts"] if row[-1] == "error")
        peer_errors = Counter(row[1] for row in peer["accounts"] if row[-1] == "error")
        unmatched = (errors - peer_errors) & present
        add("P7/credential-error", (host,), sum(unmatched.values()))
        add("P9/Cursor-overlap", (host,), len(set(observed["cursor"])
                                             & (set(m5["direct"]) | set(will["direct"]))))
        add("P9/glm-5.2", (host,), int(observed["glm_5.2_cursor"] and
                                      (m5["glm_5.2_direct"] or will["glm_5.2_direct"])))
        extra_plugins = set(map(tuple, observed["plugins"])) - set(map(tuple, peer["plugins"]))
        for scope, count in Counter(row[0] for row in extra_plugins).items():
            add("P10/" + scope, (host,), count)

    # Counts are combined across client keys before evaluating an exception.
    counts = Counter()
    for surface, hosts, count in differences:
        counts[surface, hosts] += count
    return [(surface, hosts, count) for (surface, hosts), count in sorted(counts.items())]


def evaluate(differences, exceptions, today):
    by_scope = {(row["surface"], tuple(sorted(row["hosts"]))): row for row in exceptions}
    observed_scopes = {(surface, hosts) for surface, hosts, _ in differences}
    problems = []
    accepted = []
    for surface, hosts, count in differences:
        row = by_scope.get((surface, hosts))
        result = {"surface": surface, "hosts": hosts, "count": count}
        if (row and not strict_surface(surface)
                and date.fromisoformat(row["date"]) <= today < date.fromisoformat(row["review_by"])):
            if row.get("count", count) == count:
                accepted.append(dict(result, reason=row["reason"], review_by=row["review_by"]))
                continue
        problems.append(result)
    expired = [{"surface": row["surface"], "hosts": row["hosts"]} for row in exceptions
               if date.fromisoformat(row["review_by"]) <= today]
    unused = [{"surface": surface, "hosts": row["hosts"]}
              for (surface, hosts), row in by_scope.items() if (surface, hosts) not in observed_scopes]
    return problems, accepted, expired, unused


def strict_surface(surface):
    return surface.startswith("P3/") or surface in STRICT_SURFACES


def read_json(filename):
    return json.loads(filename.read_text(encoding="utf-8"))


def validate_contract(hosts, paths, release, exceptions, required, bundle):
    if set(hosts) != set(FLEET_HOSTS):
        return False
    for name, host in hosts.items():
        if host["name"] != name or type(host["port"]) is not int or not 0 < host["port"] < 65536:
            return False
        if not valid_ssh_host(name, host["ssh"]):
            return False
    if set(paths["host_paths"]) != {"discovery", "logs-max-total-size-mb"}:
        return False
    if set(release["runtime_policy"]) != set(RUNTIME_FIELDS) - {"usage-statistics-enabled"}:
        return False
    if set(release["routing_policy"]) != {host["arch"] for host in hosts.values()}:
        return False
    will_policy = dict(release["runtime_policy"], **{"usage-statistics-enabled": False})
    for policy in release["routing_policy"].values():
        if set(policy) != set(ROUTING_FIELDS):
            return False
        if canonical(policy) != canonical(release["routing_policy"][hosts["will"]["arch"]]):
            return False
    will_policy.update({"routing." + key: val for key, val in
                        release["routing_policy"][hosts["will"]["arch"]].items()})
    if any(canonical(value) != canonical(required.get(key)) for key, value in will_policy.items()):
        return False
    pins = release["config_fingerprint"]
    if not pins["observed"] or not set(pins["observed"]) <= set(hosts):
        return False
    if any(not re.fullmatch(r"[0-9a-f]{64}", digest)
           for digest in (pins["shared"], *pins["observed"].values())):
        return False
    if not bundle or len(set(bundle)) != len(bundle):
        return False
    if any(not re.fullmatch(r"ops/[A-Za-z0-9_./-]+", path) or ".." in Path(path).parts
           or not (OPS.parent / path).is_file() for path in bundle):
        return False
    seen = set()
    for row in exceptions:
        key = row["surface"], tuple(sorted(row["hosts"]))
        prefix, _, detail = key[0].partition("/")
        if (prefix not in PARITY_PREFIXES or not detail or strict_surface(key[0])
                or not row["reason"].strip()
                or not row["decision_ref"].strip() or not key[1]
                or not set(key[1]) <= set(hosts) or key in seen
                or len(set(key[1])) != len(key[1])
                or date.fromisoformat(row["date"]) >= date.fromisoformat(row["review_by"])):
            return False
        if "count" in row and (type(row["count"]) is not int or row["count"] < 1):
            return False
        seen.add(key)
    return True


def report(command, snapshots, exceptions, today):
    """The only public projection. Private comparison material stays in memory."""
    public = {}
    for host, snapshot in snapshots.items():
        public[host] = snapshot.get("public", {"status": snapshot["status"]})
        public[host] = dict(public[host], status=snapshot["status"])
        if "error" in snapshot:
            public[host]["error"] = snapshot["error"]
    if any(item["status"] != "ok" for item in snapshots.values()):
        status = "error" if any(item["status"] == "error" for item in snapshots.values()) else "unreachable"
        return {"status": status, "hosts": public}, 2
    if command == "health":
        return {"status": "ok", "hosts": public}, 0
    if command == "fingerprint":
        snapshot = next(iter(snapshots.values()))
        differences = [(surface, tuple(snapshots), count)
                       for surface, count in sorted(snapshot["issues"].items())]
    else:
        differences = compare(snapshots["m5"], snapshots["will"])
    drift, accepted, expired, unused = evaluate(differences, exceptions, today)
    failed = bool(drift or expired)
    status = "drift" if failed else "equal" if command == "parity" else "ok"
    output = {"status": status, "hosts": public, "drift": drift,
              "exceptions": accepted, "expired_exceptions": expired}
    if command == "parity":
        output["unused_exceptions"] = unused
    return output, int(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="check tracked pins and policy without network access")
    commands.add_parser("parity", help="compare both hosts, using SSH for Will")
    for command in ("fingerprint", "health"):
        action = commands.add_parser(command)
        action.add_argument("--host", choices=FLEET_HOSTS, required=command == "fingerprint")
    args = parser.parse_args(argv)
    try:
        hosts = read_json(OPS / "fleet/hosts.json")
        paths = read_json(OPS / "fleet/paths.json")
        release = read_json(OPS / "fleet-release.json")
        exceptions = read_json(OPS / "fleet/exceptions.json")
        today = datetime.now(timezone.utc).date()
        if args.command == "check":
            required = read_json(OPS / "instances/will/config-policy.json")["required_config"]
            bundle = (OPS / "fleet/bundle.txt").read_text(encoding="utf-8").splitlines()
            valid = validate_contract(hosts, paths, release, exceptions, required, bundle)
            output = {"status": "ok" if valid else "invalid"}
            code = 0 if valid else 1
        else:
            names = (args.host,) if getattr(args, "host", None) else FLEET_HOSTS
            nonce = secrets.token_bytes(32)
            snapshots = {name: probe(name, hosts, paths, release, nonce, args.command)
                         for name in names}
            output, code = report(args.command, snapshots, exceptions, today)
        serialized = json.dumps(output, indent=2, sort_keys=True)
    except Exception:
        serialized = json.dumps(
            {"status": "error", "error": "Could not read fleet contract or probe result"},
            indent=2, sort_keys=True,
        )
        code = 2
    print(serialized)
    return code


if __name__ == "__main__" and sys.argv[1:] != ["--worker"]:
    sys.exit(main())
