#!/bin/zsh
# Bootstrap a one-shot LaunchAgent that probes SSH to Will, wait for its result, remove it.
# Usage: ssh_probe_launchd.sh <out-file> [sh|python]. `python` (the default for the monitor) runs
# ssh-probe.py with Apple's /usr/bin/python3, the monitor's own chain; `sh` runs ssh-probe.sh.
set -u
dir=${0:A:h}; out="$1"; mode="${2:-python}"
if [ "$mode" = sh ]; then program=(/bin/sh "$dir/ssh-probe.sh" "$out"); else program=(/usr/bin/python3 -B "$dir/ssh-probe.py" "$out"); fi
plist="$HOME/Library/LaunchAgents/com.rever.ssh-probe-tmp.plist"
rm -f "$out"
/usr/bin/python3 - "$plist" "${program[@]}" <<'PY'
import plistlib, sys
plist, *program = sys.argv[1:]
plistlib.dump({"Label": "com.rever.ssh-probe-tmp", "ProgramArguments": program, "RunAtLoad": True,
               "ProcessType": "Background",
               "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"}},
              open(plist, "wb"))
PY
launchctl bootstrap "gui/$(id -u)" "$plist"
for i in {1..40}; do grep -q '^done' "$out" 2>/dev/null && break; sleep 1; done
launchctl bootout "gui/$(id -u)/com.rever.ssh-probe-tmp" 2>/dev/null
rm -f "$plist"
cat "$out"
