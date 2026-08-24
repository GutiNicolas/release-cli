"""Gradle adapter: gradle.properties version= or root build.gradle(.kts) version."""

from __future__ import annotations

import re
from pathlib import Path

from release_cli.adapters.base import AdapterError, ProjectState
from release_cli.config import Config

_PROPS_VERSION = re.compile(r"^(\s*version\s*=\s*)([^\s#]+)\s*(#.*)?$")
_KTS_VERSION = re.compile(r'^(\s*)version\s*=\s*"([^"]*)"(\s*)$')
_KTS_SET = re.compile(r'^(\s*)version\.set\(\s*"([^"]*)"\s*\)(\s*)$')
_GROOVY_VERSION = re.compile(r"^(\s*)version\s*=\s*['\"]([^'\"]*)['\"](\s*)$")
_ROOT_NAME = re.compile(r"""^\s*rootProject\.name\s*=\s*['"]([^'"]+)['"]""")


def _brace_depth_delta(line: str) -> int:
    return line.count("{") - line.count("}")


def _first_root_match(text: str, patterns: tuple[re.Pattern[str], ...]) -> tuple[int, re.Match[str], re.Pattern[str]] | None:
    depth = 0
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("*"):
            depth += _brace_depth_delta(line)
            continue
        if depth == 0:
            for pattern in patterns:
                match = pattern.match(line.rstrip("\n"))
                if match:
                    return idx, match, pattern
        depth += _brace_depth_delta(line)
        if depth < 0:
            depth = 0
    return None


def _properties_version_line(text: str) -> tuple[int, re.Match[str]] | None:
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PROPS_VERSION.match(line.rstrip("\n"))
        if match:
            return idx, match
    return None


def _settings_name(cwd: Path) -> str | None:
    for name in ("settings.gradle.kts", "settings.gradle"):
        path = cwd / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _ROOT_NAME.match(line)
            if match:
                return match.group(1)
    return None


def _splice_line(text: str, line_no: int, new_line: str) -> str:
    lines = text.splitlines(keepends=True)
    if line_no < 1 or line_no > len(lines):
        raise AdapterError(f"line {line_no} out of range")
    ending = ""
    original = lines[line_no - 1]
    if original.endswith("\r\n"):
        ending = "\r\n"
    elif original.endswith("\n"):
        ending = "\n"
    lines[line_no - 1] = new_line.rstrip("\r\n") + ending
    return "".join(lines)


class GradleAdapter:
    name = "gradle"

    def discover(self, cwd: Path) -> ProjectState:
        artifact = _settings_name(cwd) or cwd.name
        props = cwd / "gradle.properties"
        if props.is_file():
            hit = _properties_version_line(props.read_text(encoding="utf-8"))
            if hit:
                return ProjectState(version=hit[1].group(2), artifact=artifact, version_file="gradle.properties")
        for name in ("build.gradle.kts", "build.gradle"):
            path = cwd / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            patterns = (_KTS_VERSION, _KTS_SET, _GROOVY_VERSION)
            found = _first_root_match(text, patterns)
            if found:
                _line, match, _pattern = found
                return ProjectState(version=match.group(2), artifact=artifact, version_file=name)
        raise AdapterError("put version in gradle.properties (version=...) or root build.gradle(.kts)")

    def read(self, cwd: Path, cfg: Config) -> ProjectState:
        discovered = self.discover(cwd)
        path = cwd / cfg.version_file
        if not path.is_file():
            raise AdapterError(f"{cfg.version_file} not found")
        if cfg.version_file == "gradle.properties":
            hit = _properties_version_line(path.read_text(encoding="utf-8"))
            if not hit:
                raise AdapterError("missing version= in gradle.properties")
            return ProjectState(version=hit[1].group(2), artifact=cfg.artifact or discovered.artifact, version_file=cfg.version_file)
        text = path.read_text(encoding="utf-8")
        found = _first_root_match(text, (_KTS_VERSION, _KTS_SET, _GROOVY_VERSION))
        if not found:
            raise AdapterError(f"missing root version in {cfg.version_file}")
        return ProjectState(
            version=found[1].group(2),
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
        if cfg.version_file == "gradle.properties":
            hit = _properties_version_line(text)
            if not hit:
                raise AdapterError("missing version= in gradle.properties")
            line_no, match = hit
            new_line = f"{match.group(1)}{version}"
            if match.group(3):
                new_line += f" {match.group(3)}"
            path.write_text(_splice_line(text, line_no, new_line), encoding="utf-8")
            return [path]
        found = _first_root_match(text, (_KTS_VERSION, _KTS_SET, _GROOVY_VERSION))
        if not found:
            raise AdapterError(f"missing root version in {cfg.version_file}")
        line_no, match, pattern = found
        original = text.splitlines()[line_no - 1]
        if pattern is _KTS_SET:
            new_line = f'{match.group(1)}version.set("{version}"){match.group(3)}'
        elif pattern is _KTS_VERSION:
            new_line = f'{match.group(1)}version = "{version}"{match.group(3)}'
        else:
            quote = "'" if "'" in original and '"' not in original.split("=")[-1] else '"'
            new_line = f"{match.group(1)}version = {quote}{version}{quote}{match.group(3)}"
        path.write_text(_splice_line(text, line_no, new_line), encoding="utf-8")
        return [path]
