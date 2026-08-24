from __future__ import annotations

from pathlib import Path

from release_cli.detect import describe_unsupported, detect


def test_detect_maven(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    assert detect(tmp_path) == ["maven"]


def test_detect_gradle(tmp_path: Path) -> None:
    (tmp_path / "settings.gradle.kts").write_text('rootProject.name = "x"', encoding="utf-8")
    assert detect(tmp_path) == ["gradle"]


def test_detect_sbt(tmp_path: Path) -> None:
    (tmp_path / "build.sbt").write_text('name := "x"', encoding="utf-8")
    assert detect(tmp_path) == ["sbt"]


def test_detect_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    (tmp_path / "build.gradle").write_text("plugins {}", encoding="utf-8")
    assert detect(tmp_path) == ["maven", "gradle"]


def test_detect_empty(tmp_path: Path) -> None:
    assert detect(tmp_path) == []
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert "package.json" in describe_unsupported(tmp_path)
