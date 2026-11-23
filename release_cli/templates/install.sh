#!/bin/sh
# Installs @BINARY@ from its GitHub Release and checks the download against checksums.txt.
#
#   curl -fsSL <release url>/install.sh | sh
#
# Settings (environment):
#   VERSION   version to install (default: the release this script was published with)
#   PREFIX    install under PREFIX/bin (default: /usr/local)
#   BASE_URL  where the release downloads live (default: this project's GitHub Releases)
set -eu

BINARY='@BINARY@'
DEFAULT_VERSION='@VERSION@'
TAG_TEMPLATE='@TAG_TEMPLATE@'
BASE_URL="${BASE_URL:-@BASE_URL@}"
VERSION="${VERSION:-$DEFAULT_VERSION}"
PREFIX="${PREFIX:-/usr/local}"

fail() {
  echo "install: $*" >&2
  exit 1
}

case "$VERSION" in
  '' | *[!0-9A-Za-z.+-]*) fail "invalid VERSION '$VERSION'" ;;
esac

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=darwin ;;
  *) fail "unsupported operating system '$(uname -s)' (Linux and macOS only)" ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 ;;
  arm64 | aarch64) arch=arm64 ;;
  *) fail "unsupported architecture '$(uname -m)' (amd64 and arm64 only)" ;;
esac

if command -v curl >/dev/null 2>&1; then
  download() { curl -fsSL -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
  download() { wget -q -O "$2" "$1"; }
else
  fail "curl or wget is required"
fi

if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
  sha256() { shasum -a 256 "$1" | awk '{print $1}'; }
else
  fail "sha256sum or shasum is required to verify the download"
fi

tag=$(printf '%s' "$TAG_TEMPLATE" | sed "s/{version}/$VERSION/")
archive="${BINARY}_${VERSION}_${os}_${arch}.tar.gz"
tmp=$(mktemp -d) || fail "cannot create a temporary folder"
trap 'rm -rf "$tmp"' EXIT
trap 'exit 1' HUP INT TERM

echo "Downloading $BINARY $VERSION for $os/$arch"
download "$BASE_URL/$tag/$archive" "$tmp/$archive" || fail "could not download $BASE_URL/$tag/$archive"
download "$BASE_URL/$tag/checksums.txt" "$tmp/checksums.txt" || fail "could not download $BASE_URL/$tag/checksums.txt"

want=$(awk -v f="$archive" '$2 == f {print $1}' "$tmp/checksums.txt")
[ -n "$want" ] || fail "$archive is not listed in checksums.txt; nothing was installed"
got=$(sha256 "$tmp/$archive")
[ "$got" = "$want" ] || fail "checksum mismatch for $archive (expected $want, got $got); nothing was installed"

mkdir "$tmp/unpacked"
tar -xzf "$tmp/$archive" -C "$tmp/unpacked" "$BINARY" || fail "could not unpack $archive"
if ! { mkdir -p "$PREFIX/bin" && install -m 0755 "$tmp/unpacked/$BINARY" "$PREFIX/bin/$BINARY"; }; then
  fail "could not write to $PREFIX/bin; run again with PREFIX set to a folder you own (PREFIX=\$HOME/.local) or with sudo"
fi

echo "Installed $BINARY $VERSION to $PREFIX/bin/$BINARY"
