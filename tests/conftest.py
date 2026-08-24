from __future__ import annotations

from pathlib import Path

from release_cli.config import dumps, parse


def write_config(tmp_path: Path, *, tool: str = "maven", artifact: str = "fraud-juggler", version_file: str = "pom.xml", hooks: str = "hooks = []") -> None:
    extra = hooks if hooks.startswith("hooks") or hooks.startswith("[[") else f"hooks = {hooks}"
    (tmp_path / ".release").write_text(
        dumps(parse(f'tool = "{tool}"\nartifact = "{artifact}"\nversion_file = "{version_file}"\n{extra}\n')),
        encoding="utf-8",
    )
