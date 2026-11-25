"""Go adapter: the version lives in a one-line plain-text file (`VERSION`), the artifact is named by go.mod."""

from __future__ import annotations

import re
from pathlib import Path

from release_cli.adapters.base import AdapterError, ProjectState
from release_cli.config import Config

DEFAULT_VERSION_FILE = "VERSION"
# `-rc` starts the next minor, so the first candidate of a fresh project is 0.1.0-rc0.
INITIAL_VERSION = "0.0.0-SNAPSHOT"
_VERSION = re.compile(r"\d+\.\d+\.\d+(-rc\d+)?(-SNAPSHOT)?")
_MODULE = re.compile(r"^module\s+(\S+)")
_MAJOR_SUFFIX = re.compile(r"v\d+")


def _parse_line(text: str, name: str) -> tuple[str, str]:
    """(version, line ending) of a file that must hold exactly one version line."""
    body = text.rstrip("\r\n") if text.endswith(("\r\n", "\n")) else text
    ending = text[len(body) :]
    if not _VERSION.fullmatch(body):
        shown = body.splitlines()[0] if body.splitlines() else ""
        raise AdapterError(f"{name} must hold one line X.Y.Z[-rcN][-SNAPSHOT]; read {shown!r}" + (" (and more lines)" if "\n" in body else ""))
    return body, ending


def module_artifact(go_mod: Path) -> str | None:
    """Last element of the module path; a trailing major-version element (`/v2`) names the module before it."""
    for line in go_mod.read_text(encoding="utf-8").splitlines():
        match = _MODULE.match(line.strip())
        if match:
            parts = [p for p in match.group(1).strip('"`').split("/") if p]
            if len(parts) > 1 and _MAJOR_SUFFIX.fullmatch(parts[-1]):
                parts.pop()
            return parts[-1] if parts else None
    return None


class GoAdapter:
    name = "go"

    def discover(self, cwd: Path) -> ProjectState:
        go_mod = cwd / "go.mod"
        artifact = (module_artifact(go_mod) if go_mod.is_file() else None) or cwd.name
        path = cwd / DEFAULT_VERSION_FILE
        # A missing VERSION is not an error here: init offers to create it with INITIAL_VERSION.
        version = _parse_line(path.read_text(encoding="utf-8"), DEFAULT_VERSION_FILE)[0] if path.is_file() else INITIAL_VERSION
        return ProjectState(version=version, artifact=artifact, version_file=DEFAULT_VERSION_FILE)

    def paths(self, cwd: Path, cfg: Config) -> list[Path]:
        return [cwd / cfg.version_file]

    def read(self, cwd: Path, cfg: Config) -> ProjectState:
        path = cwd / cfg.version_file
        if not path.is_file():
            raise AdapterError(f"{cfg.version_file} not found")
        version, _ = _parse_line(path.read_text(encoding="utf-8"), cfg.version_file)
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
        path = cwd / cfg.version_file
        _, ending = _parse_line(path.read_text(encoding="utf-8"), cfg.version_file)
        if not _VERSION.fullmatch(version):
            raise AdapterError(f"refusing to write {version!r} to {cfg.version_file}")
        path.write_text(version + ending, encoding="utf-8", newline="")
        return [path]
