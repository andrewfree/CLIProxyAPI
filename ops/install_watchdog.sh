#!/bin/zsh
set -euo pipefail

project_root=${0:A:h:h}
source_dir=${1:-"$project_root/ops"}
launch_agents_dir="$HOME/Library/LaunchAgents"
user_domain="gui/$(/usr/bin/id -u)"
labels=(com.rever.cliproxy-watchdog com.rever.cliproxy-log-archive)

staging_dir=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/cliproxy-agents.XXXXXXXX")
trap '/bin/rm -rf -- "$staging_dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for label in "${labels[@]}"; do
    /bin/cp "$source_dir/$label.plist" "$staging_dir/$label.plist"
done

for label in "${labels[@]}"; do
    source_plist="$staging_dir/$label.plist"
    /usr/bin/plutil -lint "$source_plist"
    if ! script=$(/usr/bin/plutil -extract ProgramArguments.1 raw -expect string -o - "$source_plist") ||
        [[ "$script" != /* || ! -f "$script" ]]; then
        printf 'Cannot install %s because its ProgramArguments script is missing. See the Mac ops bundle cutover in ops/FLEET.md.\n' "$label" >&2
        exit 1
    fi
    if [[ "$label" == com.rever.cliproxy-log-archive ]]; then
        if ! /usr/bin/python3 -I -c '
import plistlib
import sys

with open(sys.argv[1], "rb") as plist:
    arguments = plistlib.load(plist)["ProgramArguments"]
valid = (isinstance(arguments, list) and all(isinstance(arg, str) for arg in arguments)
         and "--archive-only" in arguments[2:] and "--prune" not in arguments[2:])
sys.exit(not valid)
' "$source_plist"; then
            printf 'Cannot install %s: ProgramArguments requires --archive-only and forbids --prune.\n' "$label" >&2
            exit 1
        fi
    fi
done

/bin/mkdir -p "$launch_agents_dir"

for label in "${labels[@]}"; do
    source_plist="$staging_dir/$label.plist"
    installed_plist="$launch_agents_dir/$label.plist"
    /usr/bin/install -m 0644 "$source_plist" "$installed_plist"

    if /bin/launchctl print "$user_domain/$label" >/dev/null 2>&1; then
        /bin/launchctl bootout "$user_domain/$label"
    fi

    /bin/launchctl bootstrap "$user_domain" "$installed_plist"
    /bin/launchctl enable "$user_domain/$label"
    /bin/launchctl print "$user_domain/$label"
done
