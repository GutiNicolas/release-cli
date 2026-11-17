from __future__ import annotations

from pathlib import Path

from release_cli.pom import apply_version, read_fields

FIXTURE = Path(__file__).parent / "fixtures" / "example-app.pom.xml"


def test_read_skips_parent_and_strips_scm_tag() -> None:
    fields = read_fields(FIXTURE.read_text(encoding="utf-8"))
    assert fields.version == "1.4.0-SNAPSHOT"
    assert fields.artifact_id == "example-app"
    assert fields.scm_tag == "example-app-1.2.0"


def test_splice_does_not_touch_matching_dependency_versions() -> None:
    original = FIXTURE.read_text(encoding="utf-8")
    updated = apply_version(original, "1.5.0-rc0", "example-app-1.5.0-rc0")

    assert "<version>1.5.0-rc0</version>" in updated
    assert "<tag>example-app-1.5.0-rc0</tag>" in updated
    assert "<version>3.5.16</version>" in updated
    assert "<commons-lib.version>1.4.0</commons-lib.version>" in updated
    assert "<events-lib.version>1.4.0</events-lib.version>" in updated
    assert "<artifactId>lib</artifactId>\n\t\t\t<version>1.4.0</version>" in updated
    assert "<artifactId>maven-compiler-plugin</artifactId>\n\t\t\t\t<version>1.4.0</version>" in updated

    original_lines = original.splitlines()
    updated_lines = updated.splitlines()
    changed = [i for i, (a, b) in enumerate(zip(original_lines, updated_lines, strict=True)) if a != b]
    assert len(changed) == 2


def test_project_tag_spanning_several_lines_keeps_line_numbers() -> None:
    # Spring Initializr style: <project ...> attributes split across lines.
    original = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0"\n'
        '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 '
        'https://maven.apache.org/xsd/maven-4.0.0.xsd">\n'
        "  <parent>\n"
        "    <version>4.1.1</version>\n"
        "    <relativePath/>\n"
        "  </parent>\n"
        "  <groupId>com.example</groupId>\n"
        "  <artifactId>demo</artifactId>\n"
        "  <version>0.0.1-SNAPSHOT</version>\n"
        "</project>\n"
    )
    updated = apply_version(original, "0.0.1-rc0", None)
    assert "  <version>0.0.1-rc0</version>\n" in updated
    assert "<version>4.1.1</version>" in updated
    assert "<groupId>com.example</groupId>" in updated


def test_next_snapshot_sets_scm_head() -> None:
    original = FIXTURE.read_text(encoding="utf-8")
    released = apply_version(original, "1.5.0-rc0", "example-app-1.5.0-rc0")
    nxt = apply_version(released, "1.5.0-rc0-SNAPSHOT", "HEAD")
    assert "<version>1.5.0-rc0-SNAPSHOT</version>" in nxt
    assert "<tag>HEAD</tag>" in nxt
    assert "<commons-lib.version>1.4.0</commons-lib.version>" in nxt
