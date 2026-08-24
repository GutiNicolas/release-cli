"""sbt adapter: version.sbt or version := in build.sbt, not libraryDependencies."""

from __future__ import annotations

import re
from pathlib import Path

from release_cli.adapters.base import AdapterError, ProjectState
from release_cli.config import Config

_VERSION = re.compile(r'^(ThisBuild\s*/\s*)?version\s*:=\s*"([^"]*)"(.*)$')
_NAME = re.compile(r'^(ThisBuild\s*/\s*)?name\s*:=\s*"([^"]*)"')


def _first_assignment(text: str, pattern: re.Pattern[str]) -> tuple[int, re.Match[str]] | None:
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("#"):
            continue
        match = pattern.match(stripped)
        if match:
            return idx, match
    return None


def _splice(text: str, line_no: int, new_stripped: str) -> str:
    lines = text.splitlines(keepends=True)
    original = lines[line_no - 1]
    indent = original[: len(original) - len(original.lstrip())]
    ending = "\n" if original.endswith("\n") else ""
    if original.endswith("\r\n"):
        ending = "\r\n"
    lines[line_no - 1] = f"{indent}{new_stripped}{ending}"
    return "".join(lines)


class SbtAdapter:
    name = "sbt"

    def _candidates(self, cwd: Path, version_file: str | None) -> list[Path]:
        if version_file:
            return [cwd / version_file]
        paths = []
        for name in ("version.sbt", "build.sbt"):
            path = cwd / name
            if path.is_file():
                paths.append(path)
        return paths

    def discover(self, cwd: Path) -> ProjectState:
        artifact = cwd.name
        name_file = cwd / "build.sbt"
        if name_file.is_file():
            hit = _first_assignment(name_file.read_text(encoding="utf-8"), _NAME)
            if hit:
                artifact = hit[1].group(2)
        for path in self._candidates(cwd, None):
            hit = _first_assignment(path.read_text(encoding="utf-8"), _VERSION)
            if hit:
                return ProjectState(version=hit[1].group(2), artifact=artifact, version_file=path.name)
        raise AdapterError('put version := "x.y.z-SNAPSHOT" in version.sbt or build.sbt')

    def read(self, cwd: Path, cfg: Config) -> ProjectState:
        discovered = self.discover(cwd)
        path = cwd / cfg.version_file
        if not path.is_file():
            raise AdapterError(f"{cfg.version_file} not found")
        hit = _first_assignment(path.read_text(encoding="utf-8"), _VERSION)
        if not hit:
            raise AdapterError(f"missing version := in {cfg.version_file}")
        return ProjectState(
            version=hit[1].group(2),
            artifact=cfg.artifact or discovered.artifact,
            version_file=cfg.version_file,
        )

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
        text = path.read_text(encoding="utf-8")
        hit = _first_assignment(text, _VERSION)
        if not hit:
            raise AdapterError(f"missing version := in {cfg.version_file}")
        line_no, match = hit
        prefix = match.group(1) or ""
        suffix = match.group(3) or ""
        new_stripped = f'{prefix}version := "{version}"{suffix}'
        path.write_text(_splice(text, line_no, new_stripped), encoding="utf-8")
        return [path]
