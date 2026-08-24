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
printf '%s\n' "Try: release --help"
