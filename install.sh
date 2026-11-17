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

bin=$(command -v release || true)
if [ -z "$bin" ]; then
    printf '%s\n' "Installed, but 'release' is not on PATH." >&2
    printf '%s\n' "Add this to your shell rc and open a new terminal:" >&2
    printf '%s\n' '  export PATH="$HOME/.local/bin:$PATH"' >&2
    exit 1
fi

printf '%s\n' "Installed: $bin"
"$bin" -h >/dev/null

# Writes ~/.config/release/source.toml (remote, branch, path of this clone) so `release update`
# works from any directory. Asks cursor-review only if its key does not exist yet; keeps existing config.
"$bin" update --after-install "$ROOT"
printf '%s\n' "Update later from anywhere: release update"
printf '%s\n' "Hooks and connectors: release edit"
printf '%s\n' "Try: release --help"
