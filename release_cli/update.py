"""`release update`: pull the release-cli clone, reinstall it with uv, ask only new or changed config keys.

Never touches connectors (they stay on their SHA), connectors.toml, or any project .release.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

from release_cli import connectors as cx
from release_cli import prompts
from release_cli.config import dump_toml, loads_toml

INSTALL_URL = "https://github.com/GutiNicolas/release-cli"
PACKAGE = "release-cli"


class UpdateError(RuntimeError):
    pass


# ---------- config registry ----------


@dataclass(frozen=True)
class Key:
    table: str
    name: str
    default: Any
    question: str
    since: int
    # config_version in which the meaning changed; users below it are asked again, seeing their value.
    changed: int | None = None
    change_note: str = ""


REGISTRY: tuple[Key, ...] = (
    Key("cursor-review", "notify", False, "cursor-review: desktop notification when a post-deploy review finishes?", since=1),
)


def config_version() -> int:
    return max((max(k.since, k.changed or 0) for k in REGISTRY), default=0)


def _ask_key(key: Key, default: Any, *, defaults: bool, ask: prompts.Ask) -> Any:
    if isinstance(key.default, bool):
        return prompts.ask_bool(key.question, bool(default), defaults=defaults, ask=ask)
    return prompts.ask_text(key.question, str(default), defaults=defaults, ask=ask)


def ensure_config(*, defaults: bool, ask: prompts.Ask = input, log: Callable[[str], None] = print) -> list[str]:
    """Ask registry keys that are missing or changed meaning; keep every other value. Returns keys written."""
    data = cx.load_release_toml()
    seen = data.get("config_version", 0)
    seen = seen if type(seen) is int else 0
    target = config_version()
    asked: list[str] = []
    for key in REGISTRY:
        table = data.setdefault(key.table, {})
        label = f"{key.table}.{key.name}"
        if key.name not in table:
            table[key.name] = _ask_key(key, key.default, defaults=defaults, ask=ask)
            asked.append(label)
        elif key.changed and seen < key.changed:
            log(f"{label} changed meaning: {key.change_note} (current value: {table[key.name]!r})")
            table[key.name] = _ask_key(key, table[key.name], defaults=defaults, ask=ask)
            asked.append(label)
    for key in REGISTRY:
        if not data.get(key.table):
            data.pop(key.table, None)
    if asked or seen < target:
        data["config_version"] = max(target, seen)
        cx.save_release_toml(data)
    return asked


# ---------- source ----------


@dataclass(frozen=True)
class Source:
    path: Path
    remote: str
    branch: str
    found_in: str


def source_toml_path() -> Path:
    return cx.config_dir() / "source.toml"


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise UpdateError(f"git {' '.join(args)} failed in {path}: {(proc.stderr or proc.stdout).strip()}")
    return proc


def _is_git(path: Path) -> bool:
    return path.is_dir() and _git(path, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0


def uv_tool_dir() -> Path:
    if os.environ.get("UV_TOOL_DIR"):
        return Path(os.environ["UV_TOOL_DIR"])
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "uv" / "tools"


def _receipt_path() -> Path | None:
    receipt = uv_tool_dir() / PACKAGE / "uv-receipt.toml"
    if not receipt.is_file():
        return None
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return None
    for req in data.get("tool", {}).get("requirements", []):
        if isinstance(req, dict) and req.get("name") == PACKAGE:
            raw = req.get("directory") or req.get("editable") or req.get("path")
            if isinstance(raw, str):
                return Path(raw)
    return None


def _describe(path: Path, found_in: str, remote: str = "", branch: str = "") -> Source:
    remote = remote or _git(path, "remote", "get-url", "origin", check=False).stdout.strip()
    branch = branch or _git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
    return Source(path=path, remote=remote, branch=branch, found_in=found_in)


def find_source() -> Source | None:
    path = source_toml_path()
    if path.is_file():
        data = loads_toml(path.read_text(encoding="utf-8"))
        clone = Path(str(data.get("path", "")))
        if data.get("path") and _is_git(clone):
            return _describe(clone, str(path), str(data.get("remote", "")), str(data.get("branch", "")))
    clone = _receipt_path()
    if clone is not None and _is_git(clone):
        return _describe(clone, "uv receipt")
    return None


def register_source(root: Path) -> Source | None:
    root = root.resolve()
    if not _is_git(root):
        return None
    src = _describe(root, "install.sh")
    target = source_toml_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_toml({"remote": src.remote, "branch": src.branch, "path": str(src.path)}), encoding="utf-8")
    return src


# ---------- update ----------


def _pyproject_version(path: Path) -> str:
    try:
        return str(tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "?"


def _installed_version() -> str | None:
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        return None


def update(*, defaults: bool, log: Callable[[str], None] = print) -> None:
    src = find_source()
    if src is None:
        raise UpdateError(
            "no release-cli source found (no ~/.config/release/source.toml, no uv receipt pointing at a git clone). "
            f"install it: git clone {INSTALL_URL} && cd release-cli && ./install.sh"
        )
    log(f"source: {src.path} ({src.found_in})")
    dirty = _git(src.path, "status", "--porcelain").stdout.strip()
    if dirty:
        raise UpdateError(f"{src.path} has local changes; commit or stash them, then run `release update` again:\n{dirty}")
    current = _git(src.path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if src.branch and current != src.branch:
        raise UpdateError(f"{src.path} is on {current}, but release-cli was installed from {src.branch}; check it out first")
    uv = shutil.which("uv")
    if uv is None:
        raise UpdateError("uv is required: curl -LsSf https://astral.sh/uv/install.sh | sh")
    old_sha = _git(src.path, "rev-parse", "--short", "HEAD").stdout.strip()
    old_version = _installed_version() or _pyproject_version(src.path)
    _git(src.path, "pull", "--ff-only", "origin", current)
    new_sha = _git(src.path, "rev-parse", "--short", "HEAD").stdout.strip()
    proc = subprocess.run([uv, "tool", "install", "--force", str(src.path)], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise UpdateError(f"uv tool install failed: {(proc.stderr or proc.stdout).strip()}")
    register_source(src.path)
    # The running process is the old code; the new binary knows the new config keys.
    fresh = shutil.which("release")
    if fresh:
        cmd = [fresh, "update", "--config-only", *(["--defaults"] if defaults else [])]
        if subprocess.run(cmd, check=False).returncode != 0:
            raise UpdateError("the new release installed, but asking the new config keys failed; run `release update --config-only`")
    else:
        ensure_config(defaults=defaults, log=log)
    log(f"release-cli {old_version} ({old_sha}) -> {_pyproject_version(src.path)} ({new_sha})")
    if old_sha == new_sha:
        log("already up to date; reinstalled")
    log("connectors were not updated; use `release connector update <name>`")
