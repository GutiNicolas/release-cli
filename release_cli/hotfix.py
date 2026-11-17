"""`release hotfix X.Y.Z`: a branch from a released final, set up so `release -rc` there ships X.Y.(Z+1).

No version-file commit: from the tag the file already reads X.Y.Z, and on a `hotfix/X.Y.Z` branch the CLI
defaults the next series to a patch bump (see `cli.py`). `plan()` stays pure.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from release_cli import adapters, gitops
from release_cli.adapters.base import AdapterError
from release_cli.config import ConfigError, load
from release_cli.ui import info
from release_cli.versioning import PlanError, hotfix_branch, parse

MERGE_BACK = (
    "merge {branch} back into your main branch (pull request or `git merge`); not done for you. "
    "The version file will conflict: keep the main branch's version."
)


def merge_back_reminder(branch: str) -> str:
    return MERGE_BACK.format(branch=branch)


def _target(tag: str, artifact: str) -> tuple[str, str]:
    """(released X.Y.Z, hotfix X.Y.Z+1) for a final tag given as X.Y.Z or <artifact>-X.Y.Z."""
    name = tag[len(artifact) + 1 :] if tag.startswith(f"{artifact}-") else tag
    try:
        base = parse(name)
    except PlanError as exc:
        raise ConfigError(f"{tag} is not a version tag") from exc
    if base.rc is not None or base.snapshot:
        raise ConfigError(f"{name} is not a final release; a hotfix starts from a final tag X.Y.Z")
    return name, replace(base, patch=base.patch + 1).format()


def _check_free(branch: str, target: str) -> None:
    taken = [t for t in gitops.tags() if t == target or t.startswith(f"{target}-rc")]
    taken += gitops.remote_tags_of(target)
    if taken:
        raise ConfigError(
            f"{target} already has tags ({', '.join(sorted(set(taken)))}); "
            f"patch from the latest one instead: release hotfix {target}"
        )
    if gitops.branch_exists_local(branch) or gitops.remote_branch_exists(branch):
        raise ConfigError(f"branch {branch} already exists (locally or on origin); check it out instead")


def hotfix(cwd: Path, tag: str) -> None:
    cfg = load(cwd)
    if cfg is None:
        raise ConfigError("no .release or release.toml here; run `release --init`")
    released, target = _target(tag, cfg.artifact)
    branch = hotfix_branch(target)

    start = gitops.current_repo()
    dirty = gitops.porcelain()
    if dirty:
        raise ConfigError(f"working tree is not clean; commit or stash first:\n{dirty}")
    if not gitops.tag_exists_local(released):
        if not gitops.remote_tag_exists(released):
            raise ConfigError(f"tag {released} not found locally or on origin")
        gitops.fetch_tag(released)
    _check_free(branch, target)

    gitops.create_branch(branch, released)
    try:
        # The adapters read the version file as it is at the tag: it must say X.Y.Z (the build config may differ there).
        at_tag = adapters.get(cfg.tool).read(cwd, cfg).version
        if at_tag != released:
            raise ConfigError(f"at tag {released} the version file reads {at_tag}, not {released}; not a release commit")
    except (AdapterError, ConfigError) as exc:
        gitops.switch(start.branch)
        gitops.delete_branch(branch)
        raise ConfigError(str(exc)) from exc

    info(f"on {branch} (from tag {released}). fix, commit, then:")
    info(f"  release -rc   -> {target}-rc0 (then -rc for rc1, rc2, ...)")
    info(f"  release -fv   -> {target}")
    info(f"it stays local until the first release pushes it. after -fv: {merge_back_reminder(branch)}")
