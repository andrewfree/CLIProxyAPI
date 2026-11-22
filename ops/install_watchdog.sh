#!/bin/zsh
set -euo pipefail

project_root=${0:A:h:h}
source_dir=${1:-"$project_root/ops"}
launch_agents_dir="$HOME/Library/LaunchAgents"
user_domain="gui/$(/usr/bin/id -u)"
labels=(com.rever.cliproxy-watchdog com.rever.cliproxy-log-archive)

for label in "${labels[@]}"; do
    source_plist="$source_dir/$label.plist"
    /usr/bin/plutil -lint "$source_plist"
    if ! script=$(/usr/bin/plutil -extract ProgramArguments.1 raw -expect string -o - "$source_plist") ||
        [[ "$script" != /* || ! -f "$script" ]]; then
        printf 'Cannot install %s because its ProgramArguments script is missing. See the Mac ops bundle cutover in ops/FLEET.md.\n' "$label" >&2
        exit 1
    fi
    if [[ "$label" == com.rever.cliproxy-log-archive ]]; then
        count=$(/usr/bin/plutil -extract ProgramArguments raw -expect array -o - "$source_plist")
        archive_only=false
        for ((i = 2; i < count; i++)); do
            argument=$(/usr/bin/plutil -extract "ProgramArguments.$i" raw -expect string -o - "$source_plist")
            case "$argument" in
                --archive-only) archive_only=true ;;
                --prune) archive_only=false; break ;;
            esac
        done
        if [[ "$archive_only" != true ]]; then
            printf 'Cannot install %s: ProgramArguments requires --archive-only and forbids --prune.\n' "$label" >&2
            exit 1
        fi
    fi
done

/bin/mkdir -p "$launch_agents_dir"

for label in "${labels[@]}"; do
    source_plist="$source_dir/$label.plist"
    installed_plist="$launch_agents_dir/$label.plist"
    /usr/bin/install -m 0644 "$source_plist" "$installed_plist"

    if /bin/launchctl print "$user_domain/$label" >/dev/null 2>&1; then
        /bin/launchctl bootout "$user_domain/$label"
    fi

    /bin/launchctl bootstrap "$user_domain" "$installed_plist"
    /bin/launchctl enable "$user_domain/$label"
    /bin/launchctl print "$user_domain/$label"
done
