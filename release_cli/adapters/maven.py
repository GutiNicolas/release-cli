"""Maven adapter: /project/version and /project/scm/tag only."""

from __future__ import annotations

from pathlib import Path

from release_cli.adapters.base import AdapterError, ProjectState
from release_cli.config import Config
from release_cli.pom import PomError, apply_version, read_fields

POM = "pom.xml"


class MavenAdapter:
    name = "maven"

    def discover(self, cwd: Path) -> ProjectState:
        return self.read(cwd, Config(tool="maven", artifact="unused", version_file=POM))

    def read(self, cwd: Path, cfg: Config) -> ProjectState:
        path = cwd / (cfg.version_file or POM)
        if not path.is_file():
            raise AdapterError(f"{path.name} not found")
        try:
            fields = read_fields(path.read_text(encoding="utf-8"))
        except PomError as exc:
            raise AdapterError(str(exc)) from exc
        return ProjectState(
            version=fields.version,
            artifact=fields.artifact_id,
            version_file=path.name,
        )

    def write(
        self,
        cwd: Path,
        cfg: Config,
        version: str,
        *,
        scm_tag: str | None = None,
    ) -> list[Path]:
        path = cwd / (cfg.version_file or POM)
        original = path.read_text(encoding="utf-8")
        try:
            path.write_text(apply_version(original, version, scm_tag), encoding="utf-8")
        except PomError as exc:
            raise AdapterError(str(exc)) from exc
        return [path]
