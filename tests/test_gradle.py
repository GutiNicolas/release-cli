from __future__ import annotations

from pathlib import Path

from release_cli.adapters.gradle import GradleAdapter
from release_cli.config import Config

PROPS = """# comment version=9.9.9
commons=1.4.0
version=1.4.0-SNAPSHOT
other=1.4.0
"""

KTS = """
plugins { id("java") }
dependencies {
    implementation("org.example:lib:1.4.0")
    val version = "not-the-project"
}
version = "1.4.0-SNAPSHOT"
"""


def test_gradle_properties_does_not_touch_other_1_4_0(tmp_path: Path) -> None:
    (tmp_path / "gradle.properties").write_text(PROPS, encoding="utf-8")
    (tmp_path / "settings.gradle.kts").write_text('rootProject.name = "fraud-juggler"\n', encoding="utf-8")
    adapter = GradleAdapter()
    cfg = Config(tool="gradle", artifact="fraud-juggler", version_file="gradle.properties")
    state = adapter.read(tmp_path, cfg)
    assert state.version == "1.4.0-SNAPSHOT"
    assert state.artifact == "fraud-juggler"
    adapter.write(tmp_path, cfg, "1.5.0-rc0")
    text = (tmp_path / "gradle.properties").read_text(encoding="utf-8")
    assert "version=1.5.0-rc0" in text
    assert "commons=1.4.0" in text
    assert "other=1.4.0" in text
    assert "# comment version=9.9.9" in text


def test_gradle_kts_root_version_skips_dependencies(tmp_path: Path) -> None:
    (tmp_path / "build.gradle.kts").write_text(KTS, encoding="utf-8")
    (tmp_path / "settings.gradle.kts").write_text('rootProject.name = "demo"\n', encoding="utf-8")
    adapter = GradleAdapter()
    discovered = adapter.discover(tmp_path)
    assert discovered.version == "1.4.0-SNAPSHOT"
    assert discovered.version_file == "build.gradle.kts"
    cfg = Config(tool="gradle", artifact="demo", version_file="build.gradle.kts")
    adapter.write(tmp_path, cfg, "1.5.0-rc0")
    text = (tmp_path / "build.gradle.kts").read_text(encoding="utf-8")
    assert 'implementation("org.example:lib:1.4.0")' in text
    assert 'version = "1.5.0-rc0"' in text
    assert 'val version = "not-the-project"' in text
