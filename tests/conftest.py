from __future__ import annotations

from pathlib import Path

import pytest

from release_cli.config import dumps, parse


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never read or write the real ~/.config/release or ~/.local/share/release."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    return home


def write_config(tmp_path: Path, *, tool: str = "maven", artifact: str = "fraud-juggler", version_file: str = "pom.xml", hooks: str = "hooks = []") -> None:
    extra = hooks if hooks.startswith("hooks") or hooks.startswith("[[") else f"hooks = {hooks}"
    (tmp_path / ".release").write_text(
        dumps(parse(f'tool = "{tool}"\nartifact = "{artifact}"\nversion_file = "{version_file}"\n{extra}\n')),
        encoding="utf-8",
    )
