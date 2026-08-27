#!/bin/zsh
set -euo pipefail

project_root=${0:A:h:h}
launch_agents_dir="$HOME/Library/LaunchAgents"
user_domain="gui/$(/usr/bin/id -u)"

/bin/mkdir -p "$launch_agents_dir"

for label in com.rever.cliproxy-watchdog com.rever.cliproxy-log-archive; do
    source_plist="$project_root/ops/$label.plist"
    installed_plist="$launch_agents_dir/$label.plist"
    /usr/bin/plutil -lint "$source_plist"
    /usr/bin/install -m 0644 "$source_plist" "$installed_plist"

    if /bin/launchctl print "$user_domain/$label" >/dev/null 2>&1; then
        /bin/launchctl bootout "$user_domain/$label"
    fi

    /bin/launchctl bootstrap "$user_domain" "$installed_plist"
    /bin/launchctl enable "$user_domain/$label"
    /bin/launchctl print "$user_domain/$label"
done
