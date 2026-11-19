#!/usr/bin/env python3
"""Run ssh to Will the way the fleet monitor does (a Python parent) and record the result."""
import subprocess
import sys

out = sys.argv[1]
with open(out, "w") as result:
    for dest in ("will.local",):
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", dest, "true"],
                           capture_output=True, text=True)
        result.write(f"{sys.executable} -> ssh {dest} rc={r.returncode} err={r.stderr.strip()[:160]}\n")
    result.write("done\n")
