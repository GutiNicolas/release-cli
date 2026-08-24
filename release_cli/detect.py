"""Offline build-tool detection from files in cwd."""

from __future__ import annotations

from pathlib import Path

from release_cli.config import TOOLS, Tool

MAVEN_MARKERS = ("pom.xml",)
GRADLE_MARKERS = (
    "settings.gradle",
    "settings.gradle.kts",
    "build.gradle",
    "build.gradle.kts",
)
SBT_MARKERS = ("build.sbt",)
SBT_DIR_MARKERS = (Path("project") / "build.properties",)

OTHER_HINTS = (
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pyproject.toml",
)


def _has_any(cwd: Path, names: tuple[str, ...]) -> bool:
    return any((cwd / name).is_file() for name in names)


def detect(cwd: Path) -> list[Tool]:
    found: list[Tool] = []
    if _has_any(cwd, MAVEN_MARKERS):
        found.append("maven")
    if _has_any(cwd, GRADLE_MARKERS):
        found.append("gradle")
    if _has_any(cwd, SBT_MARKERS) or any((cwd / rel).is_file() for rel in SBT_DIR_MARKERS):
        found.append("sbt")
    return found


def extras(cwd: Path) -> list[str]:
    hits = [name for name in OTHER_HINTS if (cwd / name).is_file()]
    hits.extend(path.name for path in cwd.glob("*.csproj"))
    return hits


def describe_unsupported(cwd: Path) -> str:
    extra = extras(cwd)
    known = ", ".join(TOOLS)
    if extra:
        return f"no maven/gradle/sbt project found (saw {', '.join(extra)}). supported: {known}"
    return f"no maven/gradle/sbt project found. supported: {known}"
