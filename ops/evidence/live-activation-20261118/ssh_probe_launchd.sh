#!/bin/zsh
# As run: bootstrap a one-shot LaunchAgent that runs ssh-probe.sh, wait for its result, remove it.
# Shows whether launchd jobs can reach Will over SSH (the monitor's path).
set -u
dir=${0:A:h}; out="$1"
plist="$HOME/Library/LaunchAgents/com.rever.ssh-probe-tmp.plist"
rm -f "$out"
/usr/bin/python3 - "$plist" "$dir/ssh-probe.sh" "$out" <<'PY'
import plistlib, sys
plist, script, out = sys.argv[1:]
plistlib.dump({"Label": "com.rever.ssh-probe-tmp", "ProgramArguments": ["/bin/sh", script, out], "RunAtLoad": True,
               "ProcessType": "Background",
               "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"}},
              open(plist, "wb"))
PY
launchctl bootstrap "gui/$(id -u)" "$plist"
for i in {1..40}; do grep -q '^done' "$out" 2>/dev/null && break; sleep 1; done
launchctl bootout "gui/$(id -u)/com.rever.ssh-probe-tmp" 2>/dev/null
rm -f "$plist"
cat "$out"
