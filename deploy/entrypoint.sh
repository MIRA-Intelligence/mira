#!/bin/sh
export MIRA_CONFIG_PATH="${MIRA_CONFIG_PATH:-$HOME/.mira/config.json}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.mira/.config}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.mira/.local/share}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.mira/.cache}"

dir="$HOME/.mira"
if [ -d "$dir" ] && [ ! -w "$dir" ]; then
    owner_uid=$(stat -c %u "$dir" 2>/dev/null || stat -f %u "$dir" 2>/dev/null)
    cat >&2 <<EOF
Error: $dir is not writable (owned by UID $owner_uid, running as UID $(id -u)).

Fix (pick one):
  Host:   sudo chown -R 1000:1000 ~/.mira
  Docker: docker run --user \$(id -u):\$(id -g) ...
  Podman: podman run --userns=keep-id ...
EOF
    exit 1
fi
exec mira "$@"
