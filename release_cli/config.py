"""Release project config: .release (local) overlays optional release.toml (team)."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Literal

LOCAL_NAME = ".release"
TEAM_NAME = "release.toml"
When = Literal["before", "after"]
Tool = Literal["maven", "gradle", "sbt"]
TOOLS: tuple[Tool, ...] = ("maven", "gradle", "sbt")
CONNECTOR_LISTS = ("order", "enabled", "disabled")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Hook:
    when: When
    cmd: str
    default: bool = True
    enabled: bool = True


@dataclass(frozen=True)
class Config:
    tool: Tool
    artifact: str
    version_file: str
    hooks: tuple[Hook, ...] = ()
    hooks_explicit: bool = True
    # [connectors]: order/enabled/disabled lists plus one table of values per connector.
    connectors: dict[str, Any] = field(default_factory=dict)


def _key(name: str) -> str:
    return name if _BARE_KEY.match(name) else json.dumps(name)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list) and all(not isinstance(v, dict) for v in value):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    raise ConfigError(f"cannot write TOML value: {value!r}")


def dump_toml(data: dict[str, Any], _prefix: str = "") -> str:
    """Tiny TOML writer for the shapes this CLI stores (scalars, lists, tables, arrays of tables)."""
    # no dates, no inline tables, no nested arrays of tables; add them when a config needs one.
    lines: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []
    arrays: list[tuple[str, list[dict[str, Any]]]] = []
    for name, value in data.items():
        if isinstance(value, dict):
            tables.append((name, value))
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            arrays.append((name, value))
        else:
            lines.append(f"{_key(name)} = {_value(value)}")
    for name, items in arrays:
        for item in items:
            lines.extend(["", f"[[{_prefix}{_key(name)}]]", dump_toml(item).rstrip("\n")])
    for name, table in tables:
        full = f"{_prefix}{_key(name)}"
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        nested = {k: v for k, v in table.items() if isinstance(v, dict)}
        if scalars or not nested:
            lines.extend(["", f"[{full}]"])
            if scalars:
                lines.append(dump_toml(scalars).rstrip("\n"))
        if nested:
            lines.append(dump_toml(nested, f"{full}.").rstrip("\n"))
    return "\n".join(lines).lstrip("\n") + "\n"


def _hook_dict(hook: Hook) -> dict[str, Any]:
    out: dict[str, Any] = {"when": hook.when, "cmd": hook.cmd}
    if not hook.default:
        out["default"] = False
    if not hook.enabled:
        out["enabled"] = False
    return out


def to_dict(cfg: Config) -> dict[str, Any]:
    data: dict[str, Any] = {"tool": cfg.tool, "artifact": cfg.artifact, "version_file": cfg.version_file}
    data["hooks"] = [_hook_dict(h) for h in cfg.hooks]
    if cfg.connectors:
        data["connectors"] = cfg.connectors
    return data


def dumps(cfg: Config) -> str:
    return dump_toml(to_dict(cfg))


def _parse_hooks(raw_hooks: Any) -> tuple[Hook, ...]:
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
        default = item.get("default", True)
        enabled = item.get("enabled", True)
        if not isinstance(default, bool) or not isinstance(enabled, bool):
            raise ConfigError("hook.default and hook.enabled must be true or false")
        hooks.append(Hook(when=when, cmd=cmd.strip(), default=default, enabled=enabled))
    return tuple(hooks)


def _parse_connectors(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("[connectors] must be a table")
    for key, value in raw.items():
        if key in CONNECTOR_LISTS:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"connectors.{key} must be a list of connector names")
        elif not isinstance(value, dict):
            raise ConfigError(f"connectors.{key} must be a table of values")
    return raw


def parse_data(data: dict[str, Any]) -> Config:
    tool = data.get("tool")
    if tool not in TOOLS:
        raise ConfigError(f"tool must be one of {', '.join(TOOLS)}")
    artifact = data.get("artifact")
    version_file = data.get("version_file")
    if not isinstance(artifact, str) or not artifact.strip():
        raise ConfigError("missing artifact")
    if not isinstance(version_file, str) or not version_file.strip():
        raise ConfigError("missing version_file")
    return Config(
        tool=tool,
        artifact=artifact.strip(),
        version_file=version_file.strip(),
        hooks=_parse_hooks(data.get("hooks", [])),
        hooks_explicit="hooks" in data,
        connectors=_parse_connectors(data.get("connectors", {})),
    )


def loads_toml(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc


def parse(text: str) -> Config:
    return parse_data(loads_toml(text))


def parse_file(path: Path) -> Config:
    return parse(path.read_text(encoding="utf-8"))


def merge_connectors(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if key in CONNECTOR_LISTS or not isinstance(merged.get(key), dict):
            merged[key] = value
        else:
            merged[key] = {**merged[key], **value}
    return merged


def merge(base: Config, overlay: Config) -> Config:
    hooks = overlay.hooks if overlay.hooks_explicit else base.hooks
    return replace(
        overlay,
        tool=overlay.tool or base.tool,
        artifact=overlay.artifact or base.artifact,
        version_file=overlay.version_file or base.version_file,
        hooks=hooks,
        hooks_explicit=True,
        connectors=merge_connectors(base.connectors, overlay.connectors),
    )


def load(cwd: Path) -> Config | None:
    team_path = cwd / TEAM_NAME
    local_path = cwd / LOCAL_NAME
    team = parse_file(team_path) if team_path.is_file() else None
    local = parse_file(local_path) if local_path.is_file() else None
    if team and local:
        return merge(team, local)
    return local or team


def load_layers(cwd: Path) -> tuple[Config | None, Config | None]:
    """(team, local) so `release edit` can say where each value comes from."""
    team_path = cwd / TEAM_NAME
    local_path = cwd / LOCAL_NAME
    return (
        parse_file(team_path) if team_path.is_file() else None,
        parse_file(local_path) if local_path.is_file() else None,
    )


def write_local(cwd: Path, cfg: Config) -> Path:
    path = cwd / LOCAL_NAME
    path.write_text(dumps(cfg), encoding="utf-8")
    return path


def write_team(cwd: Path, cfg: Config) -> Path:
    path = cwd / TEAM_NAME
    path.write_text(dumps(cfg), encoding="utf-8")
    return path


def update_file(cwd: Path, name: str, change: Callable[[dict[str, Any]], None]) -> Config:
    """Read the raw TOML of `.release` or `release.toml`, mutate it, validate, write it back.

    A missing file starts from the merged project config so required keys stay present.
    """
    path = cwd / name
    if path.is_file():
        data = loads_toml(path.read_text(encoding="utf-8"))
    else:
        merged = load(cwd)
        if merged is None:
            raise ConfigError("no .release or release.toml; run `release --init` first")
        data = to_dict(merged)
        data.pop("connectors", None)
    if name == LOCAL_NAME and "hooks" not in data:
        merged = load(cwd)
        data["hooks"] = [_hook_dict(h) for h in (merged.hooks if merged else ())]
    change(data)
    cfg = parse_data(data)
    path.write_text(dump_toml(data), encoding="utf-8")
    return cfg


def set_connector_value(cwd: Path, connector: str, key: str, value: Any, *, name: str = LOCAL_NAME) -> None:
    def change(data: dict[str, Any]) -> None:
        data.setdefault("connectors", {}).setdefault(connector, {})[key] = value

    update_file(cwd, name, change)
