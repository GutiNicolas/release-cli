from __future__ import annotations

from pathlib import Path

from release_cli.pom import apply_version, read_fields

FIXTURE = Path(__file__).parent / "fixtures" / "juggler-like.pom.xml"


def test_read_skips_parent_and_strips_scm_tag() -> None:
    fields = read_fields(FIXTURE.read_text(encoding="utf-8"))
    assert fields.version == "1.4.0-SNAPSHOT"
    assert fields.artifact_id == "fraud-juggler"
    assert fields.scm_tag == "fraud-juggler-1.2.0"


def test_splice_does_not_touch_matching_dependency_versions() -> None:
    original = FIXTURE.read_text(encoding="utf-8")
    updated = apply_version(original, "1.5.0-rc0", "fraud-juggler-1.5.0-rc0")

    assert "<version>1.5.0-rc0</version>" in updated
    assert "<tag>fraud-juggler-1.5.0-rc0</tag>" in updated
    assert "<version>3.5.16</version>" in updated
    assert "<service-commons.version>1.4.0</service-commons.version>" in updated
    assert "<risk-events-producer.version>1.4.0</risk-events-producer.version>" in updated
    assert "<artifactId>lib</artifactId>\n\t\t\t<version>1.4.0</version>" in updated
    assert "<artifactId>maven-compiler-plugin</artifactId>\n\t\t\t\t<version>1.4.0</version>" in updated

    original_lines = original.splitlines()
    updated_lines = updated.splitlines()
    changed = [i for i, (a, b) in enumerate(zip(original_lines, updated_lines)) if a != b]
    assert len(changed) == 2


def test_next_snapshot_sets_scm_head() -> None:
    original = FIXTURE.read_text(encoding="utf-8")
    released = apply_version(original, "1.5.0-rc0", "fraud-juggler-1.5.0-rc0")
    nxt = apply_version(released, "1.5.0-rc0-SNAPSHOT", "HEAD")
    assert "<version>1.5.0-rc0-SNAPSHOT</version>" in nxt
    assert "<tag>HEAD</tag>" in nxt
    assert "<service-commons.version>1.4.0</service-commons.version>" in nxt
