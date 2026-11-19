#!/usr/bin/env python3
"""Closed-port drill: run the shipped Codex wrapper with codex_cliproxy's real launch args,
primary replaced by a closed loopback port. Prints only the reply marker and the decision."""
import json, os, shlex, signal, socket, subprocess, sys, tempfile
# The launch arguments arrive on stdin and are never printed by this script. They are still on the
# wrapper's and Codex's command lines while the drill runs; a timeout kills that process group.
args_text, drill_dir = sys.stdin.read(), sys.argv[1]
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
    try:
        # Its own session, so a timeout can stop the wrapper and the Codex child it starts.
        proc = subprocess.Popen(cmd, cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    except OSError as error:
        print("drill did not start:", type(error).__name__)
        sys.exit(1)
    try:
        out, err = proc.communicate(timeout=240)
    except subprocess.TimeoutExpired as error:
        # The exception carries the full command line, so report only its type.
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        print("drill did not finish:", type(error).__name__)
        sys.exit(1)
    r = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
print("exit", r.returncode, "| marker in reply:", "CODEX_DRILL_OK" in r.stdout)
if r.returncode:
    print("stderr tail:", r.stderr[-400:].replace("\n", " "))
print("decisions:", open(log).read().strip() if os.path.exists(log) else "none")
