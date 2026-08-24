"""Release project config: .release (local) overlays optional release.toml (team)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

LOCAL_NAME = ".release"
TEAM_NAME = "release.toml"
When = Literal["before", "after"]
Tool = Literal["maven", "gradle", "sbt"]
TOOLS: tuple[Tool, ...] = ("maven", "gradle", "sbt")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Hook:
    when: When
    cmd: str


@dataclass(frozen=True)
class Config:
    tool: Tool
    artifact: str
    version_file: str
    hooks: tuple[Hook, ...] = ()
    hooks_explicit: bool = True


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def dumps(cfg: Config) -> str:
    lines = [
        f'tool = "{_esc(cfg.tool)}"',
        f'artifact = "{_esc(cfg.artifact)}"',
        f'version_file = "{_esc(cfg.version_file)}"',
    ]
    if not cfg.hooks:
        lines.append("hooks = []")
    else:
        for hook in cfg.hooks:
            lines.extend(
                [
                    "",
                    "[[hooks]]",
                    f'when = "{hook.when}"',
                    f'cmd = "{_esc(hook.cmd)}"',
                ]
            )
    return "\n".join(lines) + "\n"


def parse(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    tool = data.get("tool")
    if tool not in TOOLS:
        raise ConfigError(f"tool must be one of {', '.join(TOOLS)}")
    artifact = data.get("artifact")
    version_file = data.get("version_file")
    if not isinstance(artifact, str) or not artifact.strip():
        raise ConfigError("missing artifact")
    if not isinstance(version_file, str) or not version_file.strip():
        raise ConfigError("missing version_file")
    raw_hooks = data.get("hooks", [])
    if not isinstance(raw_hooks, list):
        raise ConfigError("hooks must be a list")
    hooks: list[Hook] = []
    for item in raw_hooks:
        if not isinstance(item, dict):
            raise ConfigError("invalid hook")
        when = item.get("when")
        cmd = item.get("cmd")
        if when not in ("before", "after"):
            raise ConfigError('hook.when must be "before" or "after"')
        if not isinstance(cmd, str) or not cmd.strip():
            raise ConfigError("hook.cmd is empty")
        hooks.append(Hook(when=when, cmd=cmd.strip()))
    return Config(
        tool=tool,
        artifact=artifact.strip(),
        version_file=version_file.strip(),
        hooks=tuple(hooks),
        hooks_explicit="hooks" in data,
    )


def parse_file(path: Path) -> Config:
    return parse(path.read_text(encoding="utf-8"))


def merge(base: Config, overlay: Config) -> Config:
    hooks = overlay.hooks if overlay.hooks_explicit else base.hooks
    return replace(
        overlay,
        tool=overlay.tool or base.tool,
        artifact=overlay.artifact or base.artifact,
        version_file=overlay.version_file or base.version_file,
        hooks=hooks,
        hooks_explicit=True,
    )


def load(cwd: Path) -> Config | None:
    team_path = cwd / TEAM_NAME
    local_path = cwd / LOCAL_NAME
    team = parse_file(team_path) if team_path.is_file() else None
    local = parse_file(local_path) if local_path.is_file() else None
    if team and local:
        return merge(team, local)
    return local or team


def write_local(cwd: Path, cfg: Config) -> Path:
    path = cwd / LOCAL_NAME
    path.write_text(dumps(cfg), encoding="utf-8")
    return path


def write_team(cwd: Path, cfg: Config) -> Path:
    path = cwd / TEAM_NAME
    path.write_text(dumps(cfg), encoding="utf-8")
    return path
