"""Build-tool adapter protocol."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from release_cli.config import Config, Tool


class AdapterError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectState:
    version: str
    artifact: str
    version_file: str


class Adapter(Protocol):
    name: Tool

    def discover(self, cwd: Path) -> ProjectState: ...

    def read(self, cwd: Path, cfg: Config) -> ProjectState: ...

    def write(
        self,
        cwd: Path,
        cfg: Config,
        version: str,
        *,
        scm_tag: str | None = None,
    ) -> list[Path]: ...
