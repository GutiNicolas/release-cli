from __future__ import annotations

from release_cli.adapters.base import Adapter, AdapterError, ProjectState
from release_cli.adapters.gradle import GradleAdapter
from release_cli.adapters.maven import MavenAdapter
from release_cli.adapters.sbt import SbtAdapter
from release_cli.config import Tool

_ADAPTERS: dict[Tool, Adapter] = {
    "maven": MavenAdapter(),
    "gradle": GradleAdapter(),
    "sbt": SbtAdapter(),
}


def get(tool: Tool) -> Adapter:
    try:
        return _ADAPTERS[tool]
    except KeyError as exc:
        raise AdapterError(f"unknown tool: {tool}") from exc
