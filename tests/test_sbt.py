from __future__ import annotations

from pathlib import Path

from release_cli.adapters.sbt import SbtAdapter
from release_cli.config import Config

BUILD = """
name := "fraud-juggler"
version := "1.4.0-SNAPSHOT"
libraryDependencies += "org.example" %% "lib" % "1.4.0"
"""


def test_sbt_version_does_not_touch_library_dependencies(tmp_path: Path) -> None:
    (tmp_path / "build.sbt").write_text(BUILD, encoding="utf-8")
    adapter = SbtAdapter()
    discovered = adapter.discover(tmp_path)
    assert discovered.version == "1.4.0-SNAPSHOT"
    assert discovered.artifact == "fraud-juggler"
    cfg = Config(tool="sbt", artifact="fraud-juggler", version_file="build.sbt")
    adapter.write(tmp_path, cfg, "1.5.0-rc0")
    text = (tmp_path / "build.sbt").read_text(encoding="utf-8")
    assert 'version := "1.5.0-rc0"' in text
    assert '"org.example" %% "lib" % "1.4.0"' in text
    assert 'name := "fraud-juggler"' in text
