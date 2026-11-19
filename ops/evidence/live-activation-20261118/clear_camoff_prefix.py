#!/usr/bin/env python3
"""Clear the camoff-astra prefix on the Codex credential whose label hash is argv[1].
Run from the instance root (reads .env for the management key; never prints it)."""
import hashlib, json, sys, urllib.parse, urllib.request
target_hash = sys.argv[1]
env = dict(l.split("=", 1) for l in open(".env").read().splitlines() if "=" in l and not l.lstrip().startswith("#"))
key = env["MANAGEMENT_PASSWORD"].strip().strip('"').strip("'")
def call(method, path, body=None):
    req = urllib.request.Request("http://127.0.0.1:8317/v8/management" + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=20))
h = lambda s: hashlib.sha256(str(s).lower().encode()).hexdigest()[:10]
target = [f for f in call("GET", "/credentials")["files"] if f.get("provider") == "codex" and h(f["label"]) == target_hash]
assert len(target) == 1, len(target)
cid = target[0]["id"]
models = lambda: [m["id"] for m in call("GET", "/credentials/models?" + urllib.parse.urlencode({"name": cid}))["models"]]
before = models()
print("before:", len(before), sorted({i.split("/", 1)[0] for i in before if "/" in i}))
print("patch status:", call("PATCH", "/credentials/fields", {"name": cid, "prefix": ""}).get("status"))
after = models()
print("after:", len(after), sorted({i.split("/", 1)[0] for i in after if "/" in i}),
      "unprefixed kept:", {i for i in before if "/" not in i} <= set(after))
