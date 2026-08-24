"""Pure version contract. No disk, no git, no Maven."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

Bump = Literal["major", "minor", "patch"]


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectVersion:
    major: int
    minor: int
    patch: int
    rc: int | None = None
    snapshot: bool = False

    def format(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.rc is not None:
            text += f"-rc{self.rc}"
        if self.snapshot:
            text += "-SNAPSHOT"
        return text


@dataclass(frozen=True)
class ReleasePlan:
    release: ProjectVersion
    next_snapshot: ProjectVersion


def parse(raw: str) -> ProjectVersion:
    text = raw.strip()
    snapshot = text.endswith("-SNAPSHOT")
    core = text[: -len("-SNAPSHOT")] if snapshot else text
    parts = core.split("-rc", 1)
    base = parts[0]
    rc: int | None = None
    if len(parts) == 2:
        rc_token = parts[1]
        if rc_token.startswith("."):
            rc_token = rc_token[1:]
        if not rc_token.isdigit():
            raise PlanError(f"invalid version: {raw}")
        rc = int(rc_token)
    bits = base.split(".")
    if len(bits) != 3 or not all(b.isdigit() for b in bits):
        raise PlanError(f"invalid version: {raw}")
    return ProjectVersion(int(bits[0]), int(bits[1]), int(bits[2]), rc=rc, snapshot=snapshot)


def plan(
    current: ProjectVersion,
    *,
    final: bool,
    bump: Bump | None = None,
    explicit: str | None = None,
) -> ReleasePlan:
    if bump and explicit:
        raise PlanError("use either --major/--minor/--patch or an explicit version, not both")

    if final:
        if current.rc is None:
            raise PlanError(
                "current version is not a release candidate; cannot package a final release"
            )
        if bump or explicit:
            raise PlanError("--major/--minor/--patch and explicit versions apply to -rc, not -fv")
        release = replace(current, rc=None, snapshot=False)
        return ReleasePlan(release, replace(release, snapshot=True))

    if current.rc is not None:
        if bump or explicit:
            raise PlanError(
                f"a release candidate is already in progress ({current.format()}). "
                "use `release -rc` to increment it or `release -fv` to finalize"
            )
        release = replace(current, rc=current.rc + 1, snapshot=False)
        return ReleasePlan(release, replace(release, snapshot=True))

    if explicit:
        base = parse(explicit)
        if base.rc is not None or base.snapshot:
            raise PlanError("explicit version must be X.Y.Z")
        release = replace(base, rc=0, snapshot=False)
        return ReleasePlan(release, replace(release, snapshot=True))

    kind: Bump = bump or "minor"
    release = replace(_bump_core(current, kind), rc=0, snapshot=False)
    return ReleasePlan(release, replace(release, snapshot=True))


def _bump_core(current: ProjectVersion, kind: Bump) -> ProjectVersion:
    if kind == "major":
        return ProjectVersion(current.major + 1, 0, 0)
    if kind == "minor":
        return ProjectVersion(current.major, current.minor + 1, 0)
    return ProjectVersion(current.major, current.minor, current.patch + 1)
