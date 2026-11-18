"""Secrets (Jira API token, Slack webhook URL): environment, then the OS keychain. Never a TOML file or a log line.

Lookup order for `get(name)`: env `RELEASE_SECRET_<NAME>` (upper case, `-` becomes `_`), macOS Keychain
(`security`), then `secret-tool` (libsecret on Linux). Config files hold secret *names*, never values.

The keychains cannot list their items by service, so `release secret ls` reads a small names-only index kept
beside the global config. The index never holds a value.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from release_cli.connectors import config_dir

SERVICE = "release-cli"
ENV_PREFIX = "RELEASE_SECRET_"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TIMEOUT = 30


class SecretError(Exception):
    pass


def check_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise SecretError(f"invalid secret name {name!r}: lowercase letters, digits and '-', e.g. jira-token")
    return name


def env_var(name: str) -> str:
    return ENV_PREFIX + check_name(name).upper().replace("-", "_")


def _run(cmd: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str] | None:
    """Run a keychain tool; None when it is missing or hangs. Output stays captured (it may hold a value)."""
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _has(tool: str) -> bool:
    return shutil.which(tool) is not None


def _lookup(cmd: list[str]) -> str | None:
    proc = _run(cmd)
    if proc is None or proc.returncode != 0:
        return None
    value = proc.stdout.rstrip("\r\n")
    return value or None


def get(name: str) -> str | None:
    """The secret's value, or None when it is set nowhere. Raises SecretError only for an invalid name."""
    value = os.environ.get(env_var(name))
    if value:
        return value
    if _has("security"):
        value = _lookup(["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"])
        if value:
            return value
    if _has("secret-tool"):
        return _lookup(["secret-tool", "lookup", "service", SERVICE, "account", name])
    return None


def set_(name: str, value: str) -> str:
    """Store in the first keychain available; returns its name. The value never goes on a command line."""
    check_name(name)
    if not value:
        raise SecretError("empty value; nothing stored")
    if not value.isascii() or not value.isprintable():
        raise SecretError("the value has characters a token or webhook URL does not (non-ASCII or control); nothing stored")
    if _has("security"):
        # `security -i` reads its commands from stdin, and -X takes the value hex-encoded: neither shows in `ps`.
        line = f"add-generic-password -U -s {SERVICE} -a {name} -X {value.encode('utf-8').hex()}\n"
        proc = _run(["security", "-i"], stdin=line)
        if proc is None or proc.returncode != 0:
            raise SecretError("the macOS keychain refused the secret (locked or access denied)")
        _index_add(name)
        return "macOS keychain"
    if _has("secret-tool"):
        proc = _run(["secret-tool", "store", f"--label={SERVICE} {name}", "service", SERVICE, "account", name], stdin=value)
        if proc is None or proc.returncode != 0:
            raise SecretError("secret-tool could not store the secret (no unlocked keyring?)")
        _index_add(name)
        return "secret-tool keyring"
    raise SecretError(f"no keychain here (security or secret-tool); set the environment variable {env_var(name)} instead")


def rm(name: str) -> bool:
    """Remove from every keychain; True when something was removed."""
    check_name(name)
    removed = False
    if _has("security"):
        proc = _run(["security", "delete-generic-password", "-s", SERVICE, "-a", name])
        removed = proc is not None and proc.returncode == 0
    if _has("secret-tool"):
        found = _lookup(["secret-tool", "lookup", "service", SERVICE, "account", name]) is not None
        proc = _run(["secret-tool", "clear", "service", SERVICE, "account", name])
        removed = removed or (found and proc is not None and proc.returncode == 0)
    was_indexed = name in _index()
    _index_write([n for n in _index() if n != name])
    return removed or was_indexed


def names() -> list[str]:
    """Names (never values) of the secrets set with `release secret set`, plus those given by environment."""
    found = set(_index())
    for key in os.environ:
        name = key[len(ENV_PREFIX):].lower().replace("_", "-")
        if key.startswith(ENV_PREFIX) and os.environ[key] and NAME_RE.match(name):
            found.add(name)
    return sorted(found)


def _index_path() -> Path:
    return config_dir() / "secrets.index"


def _index() -> list[str]:
    try:
        lines = _index_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line for line in lines if NAME_RE.match(line)]


def _index_write(entries: list[str]) -> None:
    path = _index_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{n}\n" for n in sorted(set(entries))), encoding="utf-8")
    except OSError as exc:
        raise SecretError(f"cannot write {path}: {exc.strerror or exc}") from exc


def _index_add(name: str) -> None:
    _index_write([*_index(), name])
