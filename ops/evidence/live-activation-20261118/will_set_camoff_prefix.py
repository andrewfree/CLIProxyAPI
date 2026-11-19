#!/usr/bin/env python3
"""As run on Will over `ssh will.local 'cd ~/cliproxy && python3 - <label-hash>' < will_set_camoff_prefix.py`.
Sets prefix camoff-astra on Will's Codex credential whose label hash matches the Mac's
camoff-astra credential. Reads the management key from Will's .env and never prints it;
prints only model counts and prefixes. <label-hash> is sha256(label.lower())[:10] of the Mac's
camoff-astra credential, computed at run time from the Mac's credential list; it is not committed."""
import hashlib
import json
import sys
import urllib.parse
import urllib.request

TARGET_LABEL_HASH = sys.argv[1]
env = dict(l.split("=", 1) for l in open(".env").read().splitlines() if "=" in l and not l.lstrip().startswith("#"))
key = env["MANAGEMENT_PASSWORD"].strip().strip('"').strip("'")


def call(method, path, body=None):
    req = urllib.request.Request("http://127.0.0.1:8317/v8/management" + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=20))


label_hash = lambda s: hashlib.sha256(str(s).lower().encode()).hexdigest()[:10]
target = [f for f in call("GET", "/credentials")["files"]
          if f.get("provider") == "codex" and label_hash(f["label"]) == TARGET_LABEL_HASH]
assert len(target) == 1, len(target)
cid = target[0]["id"]
models = lambda: [m["id"] for m in call("GET", "/credentials/models?" + urllib.parse.urlencode({"name": cid}))["models"]]
before = models()
print("before:", len(before), sorted({i.split("/", 1)[0] for i in before if "/" in i}))
print("patch status:", call("PATCH", "/credentials/fields", {"name": cid, "prefix": "camoff-astra"}).get("status"))
after = models()
print("after:", len(after), sorted({i.split("/", 1)[0] for i in after if "/" in i}), "unprefixed kept:", set(before) <= set(after))
