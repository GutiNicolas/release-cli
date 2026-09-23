#!/bin/sh
# Install the `release` command (macOS + Linux). Requires uv.
set -eu

if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' "uv is required." >&2
    printf '%s\n' "Install it: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    printf '%s\n' "Then re-run this script." >&2
    exit 1
fi

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
uv tool install --force "$ROOT"

comment_alias() {
    rcfile=$1
    [ -f "$rcfile" ] || return 0
    if grep -q "alias release=" "$rcfile" 2>/dev/null; then
        tmp="${rcfile}.release-cli.tmp"
        awk '
            /^[[:space:]]*alias release=/ && !seen {
                print "# release-cli: disabled old alias; command is now on PATH"
                print "# " $0
                seen=1
                next
            }
            { print }
        ' "$rcfile" > "$tmp"
        mv "$tmp" "$rcfile"
        printf '%s\n' "Commented alias release= in $rcfile"
    fi
}

comment_alias "${ZDOTDIR:-$HOME}/.zshrc"
comment_alias "$HOME/.bashrc"

bin=$(command -v release || true)
if [ -z "$bin" ]; then
    printf '%s\n' "Installed, but 'release' is not on PATH." >&2
    printf '%s\n' "Add this to your shell rc and open a new terminal:" >&2
    printf '%s\n' '  export PATH="$HOME/.local/bin:$PATH"' >&2
    exit 1
fi

printf '%s\n' "Installed: $bin"
"$bin" -h >/dev/null

# Ask once. Everything is editable later with `release edit`.
config="${XDG_CONFIG_HOME:-$HOME/.config}/release/connectors.toml"
if [ -t 0 ] && ! grep -q '^\[connectors\.cursor-review\]' "$config" 2>/dev/null; then
    printf '%s' "Enable cursor-review (local Cursor agent looks at errors 10 min after a deploy; desktop notification)? (y/n) [n]: "
    read -r answer || answer=""
    case "$answer" in
        y|Y|yes|YES) "$bin" connector add cursor-review --defaults ;;
        *) printf '%s\n' "cursor-review off. Enable later: release connector add cursor-review" ;;
    esac
fi
printf '%s\n' "Hooks and connectors: release edit"
printf '%s\n' "Try: release --help"
