from __future__ import annotations

import pytest

from release_cli.versioning import PlanError, parse, plan


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2.74.0-SNAPSHOT", "2.74.0-SNAPSHOT"),
        ("2.74.0-rc2-SNAPSHOT", "2.74.0-rc2-SNAPSHOT"),
        ("2.74.0-rc.2-SNAPSHOT", "2.74.0-rc2-SNAPSHOT"),
        ("2.74.0", "2.74.0"),
        (" 1.4.0-rc0 ", "1.4.0-rc0"),
    ],
)
def test_parse_roundtrip(raw: str, expected: str) -> None:
    assert parse(raw).format() == expected


def test_rc_increments_existing_series() -> None:
    current = parse("2.74.0-rc2-SNAPSHOT")
    result = plan(current, final=False)
    assert result.release.format() == "2.74.0-rc3"
    assert result.next_snapshot.format() == "2.74.0-rc3-SNAPSHOT"


def test_rc_default_minor_when_starting_series() -> None:
    current = parse("2.74.0-SNAPSHOT")
    result = plan(current, final=False)
    assert result.release.format() == "2.75.0-rc0"
    assert result.next_snapshot.format() == "2.75.0-rc0-SNAPSHOT"


def test_rc_major_patch_explicit() -> None:
    current = parse("2.74.0-SNAPSHOT")
    assert plan(current, final=False, bump="major").release.format() == "3.0.0-rc0"
    assert plan(current, final=False, bump="patch").release.format() == "2.74.1-rc0"
    assert plan(current, final=False, explicit="2.80.0").release.format() == "2.80.0-rc0"


def test_fv_strips_rc_and_keeps_same_snapshot() -> None:
    current = parse("2.74.0-rc2-SNAPSHOT")
    result = plan(current, final=True)
    assert result.release.format() == "2.74.0"
    assert result.next_snapshot.format() == "2.74.0-SNAPSHOT"


def test_fv_without_rc_errors() -> None:
    with pytest.raises(PlanError, match="not a release candidate"):
        plan(parse("2.74.0-SNAPSHOT"), final=True)


def test_flags_during_rc_error() -> None:
    current = parse("2.74.0-rc2-SNAPSHOT")
    with pytest.raises(PlanError, match="already in progress"):
        plan(current, final=False, bump="major")
    with pytest.raises(PlanError, match="already in progress"):
        plan(current, final=False, explicit="2.80.0")


def test_bump_and_explicit_conflict() -> None:
    with pytest.raises(PlanError, match="not both"):
        plan(parse("2.74.0-SNAPSHOT"), final=False, bump="minor", explicit="2.80.0")
