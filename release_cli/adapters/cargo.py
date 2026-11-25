"""Cargo adapter: the version lives in `Cargo.toml` (and `Cargo.lock` when committed); edits are plain text, never a `cargo` call."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from release_cli import gitops
from release_cli.adapters.base import AdapterError, ProjectState
from release_cli.config import Config

DEFAULT_VERSION_FILE = "Cargo.toml"
LOCK_NAME = "Cargo.lock"
# `-rc` starts the next minor, so the first candidate of a fresh project is 0.1.0-rc0.
_VERSION = re.compile(r"\d+\.\d+\.\d+(-rc\d+)?(-SNAPSHOT)?")
_HEADER = re.compile(r"^\s*(\[\[?)\s*([^\[\]]+?)\s*\]\]?\s*(#.*)?$")
_VERSION_LINE = re.compile(r"^(\s*version\s*=\s*)(\"[^\"\\]*\"|'[^']*')(.*)$", re.DOTALL)
_NAME_LINE = re.compile(r"^\s*name\s*=\s*\"([^\"]*)\"")


def _load(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AdapterError(f"{path.name} not found") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AdapterError(f"cannot parse {path}: {exc}") from exc


def _members(root: Path, data: dict[str, Any]) -> list[Path]:
    """Manifest paths of the workspace members, root package first; globs expanded, `exclude` removed."""
    out: list[Path] = [root / DEFAULT_VERSION_FILE] if "package" in data else []
    ws = data.get("workspace") or {}

    def expand(patterns: list[str]) -> set[Path]:
        return {p.resolve() for pattern in patterns for p in root.glob(pattern) if (p / DEFAULT_VERSION_FILE).is_file()}

    excluded = expand(ws.get("exclude") or [])
    for found in sorted(expand(ws.get("members") or []) - excluded):
        manifest = found / DEFAULT_VERSION_FILE
        if manifest.resolve() != (root / DEFAULT_VERSION_FILE).resolve() and manifest not in out:
            out.append(manifest)
    return out


def _inherits(package: dict[str, Any]) -> bool:
    version = package.get("version")
    return isinstance(version, dict) and version.get("workspace") is True


def _read_version(root: Path, manifest: Path) -> tuple[str, list[str]]:
    """(version, names of the packages that follow it). Fails on a member that sets its own version."""
    data = _load(manifest)
    shared = ((data.get("workspace") or {}).get("package") or {}).get("version")
    root_package = data.get("package") or {}
    # The root [package] is the source only when there is no [workspace.package] version.
    version = shared if shared is not None else root_package.get("version")
    if not isinstance(version, str):
        raise AdapterError(f"{manifest.name}: no version found; set [workspace.package] version (or [package] version)")
    if not _VERSION.fullmatch(version):
        raise AdapterError(f"{manifest.name}: version must be X.Y.Z[-rcN][-SNAPSHOT]; read {version!r}")
    followers: list[str] = []
    for member in _members(root, data):
        package = root_package if member == manifest else (_load(member).get("package") or {})
        literal = package.get("version")
        is_source = member == manifest and shared is None
        if isinstance(literal, str) and not is_source:
            raise AdapterError(f"{_shown(root, member)} sets its own version {literal!r}; use version.workspace = true (one version per repository)")
        if (is_source or _inherits(package)) and isinstance(package.get("name"), str):
            followers.append(package["name"])
    return version, followers


def _shown(root: Path, manifest: Path) -> str:
    try:
        return str(manifest.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(manifest)


def _bins(root: Path, data: dict[str, Any]) -> list[str]:
    """Binary names: each `[[bin]]`, or the package name for a crate with `src/main.rs` and no `[[bin]]`."""
    names: list[str] = []
    for member in _members(root, data):
        manifest = _load(member)
        explicit = [b["name"] for b in manifest.get("bin") or [] if isinstance(b, dict) and isinstance(b.get("name"), str)]
        package_name = (manifest.get("package") or {}).get("name")
        if explicit:
            names.extend(explicit)
        elif isinstance(package_name, str) and (member.parent / "src" / "main.rs").is_file():
            names.append(package_name)
    return names


def _edit_version(text: str, table: str, version: str, label: str) -> str:
    """Replace the value of the one `version = "..."` line inside `[table]`, keeping everything else byte for byte."""
    current = ""
    hits: list[int] = []
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        header = _HEADER.match(line)
        if header:
            current = header.group(2).replace(" ", "") if header.group(1) == "[" else ""
        elif current == table and _VERSION_LINE.match(line):
            hits.append(i)
    if len(hits) != 1:
        raise AdapterError(f"{label}: expected one `version = \"...\"` line in [{table}], found {len(hits)}")
    match = _VERSION_LINE.match(lines[hits[0]])
    assert match is not None
    quote = match.group(2)[0]
    lines[hits[0]] = f"{match.group(1)}{quote}{version}{quote}{match.group(3)}"
    return "".join(lines)


def _edit_lock(text: str, names: set[str], version: str) -> str:
    """Set `version` on the `[[package]]` blocks named in `names` that have no `source` (path packages)."""
    lines = text.splitlines(keepends=True)
    start = None
    blocks: list[tuple[int, int]] = []
    for i, line in enumerate(lines + ["[end]"]):
        if line.lstrip().startswith("["):
            if start is not None:
                blocks.append((start, i))
            start = i if line.strip() == "[[package]]" else None
    for lo, hi in blocks:
        body = lines[lo:hi]
        name = next((m.group(1) for line in body if (m := _NAME_LINE.match(line))), None)
        if name not in names or any(line.lstrip().startswith("source") for line in body):
            continue
        for offset, line in enumerate(body):
            match = _VERSION_LINE.match(line)
            if match:
                lines[lo + offset] = f"{match.group(1)}\"{version}\"{match.group(3)}"
                break
    return "".join(lines)


def _tracked(path: Path) -> bool:
    """The lock exists and git tracks it; outside a git work tree, existing is enough."""
    if not path.is_file():
        return False
    probe = gitops.run(["git", "-C", str(path.parent), "ls-files", "--error-unmatch", path.name], check=False)
    inside = gitops.run(["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"], check=False)
    return probe.returncode == 0 or inside.returncode != 0


def _read_text(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as fh:
        return fh.read()


class CargoAdapter:
    name = "cargo"

    def discover(self, cwd: Path) -> ProjectState:
        manifest = cwd / DEFAULT_VERSION_FILE
        version, _ = _read_version(cwd, manifest)
        data = _load(manifest)
        name = (data.get("package") or {}).get("name")
        if not isinstance(name, str):
            bins = _bins(cwd, data)
            members = [(_load(m).get("package") or {}).get("name") for m in _members(cwd, data)]
            name = bins[0] if len(bins) == 1 else members[0] if len(members) == 1 else cwd.name
        return ProjectState(version=version, artifact=str(name), version_file=DEFAULT_VERSION_FILE)

    def paths(self, cwd: Path, cfg: Config) -> list[Path]:
        manifest = cwd / cfg.version_file
        lock = manifest.parent / LOCK_NAME
        return [manifest, lock] if _tracked(lock) else [manifest]

    def read(self, cwd: Path, cfg: Config) -> ProjectState:
        manifest = cwd / cfg.version_file
        version, _ = _read_version(manifest.parent, manifest)
        return ProjectState(version=version, artifact=cfg.artifact, version_file=cfg.version_file)

    def write(
        self,
        cwd: Path,
        cfg: Config,
        version: str,
        *,
        scm_tag: str | None = None,
    ) -> list[Path]:
        del scm_tag
        if not _VERSION.fullmatch(version):
            raise AdapterError(f"refusing to write {version!r} to {cfg.version_file}")
        manifest = cwd / cfg.version_file
        _, followers = _read_version(manifest.parent, manifest)
        data = _load(manifest)
        table = "workspace.package" if ((data.get("workspace") or {}).get("package") or {}).get("version") is not None else "package"
        text = _edit_version(_read_text(manifest), table, version, manifest.name)
        lock = manifest.parent / LOCK_NAME
        lock_text = _edit_lock(_read_text(lock), set(followers), version) if _tracked(lock) else None
        # Everything is computed before the first write, so a failure leaves both files alone.
        manifest.write_text(text, encoding="utf-8", newline="")
        changed = [manifest]
        if lock_text is not None:
            lock.write_text(lock_text, encoding="utf-8", newline="")
            changed.append(lock)
        return changed
