#!/usr/bin/env python3
"""Closed-port drill: run the shipped Codex wrapper with codex_cliproxy's real launch args,
primary replaced by a closed loopback port. Prints only the reply marker and the decision."""
import json, os, shlex, socket, subprocess, sys, tempfile
args_text, drill_dir = sys.argv[1], sys.argv[2]
argv = shlex.split(args_text)
key = "model_providers.cliproxy.base_url="
hits = [i for i, a in enumerate(argv) if a.startswith(key)]
assert len(hits) == 1, "expected exactly one base_url override"
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
argv[hits[0]] = f'{key}"http://127.0.0.1:{port}/v1"'
log = os.path.join(drill_dir, "codex-failover.jsonl")
env = dict(os.environ, CLIPROXY_FALLBACK_URL="http://127.0.0.1:8317", CODEX_REAL_BINARY="/opt/homebrew/bin/codex",
           CLIPROXY_CODEX_FAILOVER_LOG=log)
cmd = [os.path.expanduser("~/cliproxy-live/ops/codex_fallback_launcher.mjs"), *argv,
       "-c", 'model_reasoning_effort="low"', "exec", "--skip-git-repo-check", "-s", "read-only",
       "Reply with exactly the text CODEX_DRILL_OK and nothing else."]
with tempfile.TemporaryDirectory() as work:
    r = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL)
print("exit", r.returncode, "| marker in reply:", "CODEX_DRILL_OK" in r.stdout)
if r.returncode:
    print("stderr tail:", r.stderr[-400:].replace("\n", " "))
print("decisions:", open(log).read().strip() if os.path.exists(log) else "none")
