"""Git helpers. All subprocesses go through `run` so tests can mock them."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


class GitError(RuntimeError):
    pass


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def _out(args: list[str]) -> str:
    try:
        return run(args).stdout.strip()
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or "").strip()
        raise GitError(err or f"command failed: {' '.join(args)}") from exc


@dataclass(frozen=True)
class RepoState:
    sha: str
    branch: str


def current_repo() -> RepoState:
    try:
        run(["git", "rev-parse", "--is-inside-work-tree"])
    except subprocess.CalledProcessError as exc:
        raise GitError("not a git repository") from exc
    sha = _out(["git", "rev-parse", "HEAD"])
    branch = _out(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if branch == "HEAD":
        raise GitError("detached HEAD; checkout a branch before releasing")
    return RepoState(sha=sha, branch=branch)


def porcelain() -> str:
    return _out(["git", "status", "--porcelain"])


def tag_exists_local(name: str) -> bool:
    result = run(["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{name}"], check=False)
    return result.returncode == 0


def remote_tag_exists(name: str) -> bool:
    result = run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{name}"], check=False)
    if result.returncode != 0:
        raise GitError((result.stderr or "git ls-remote failed").strip())
    return bool(result.stdout.strip())


def commit(paths: list[str], message: str) -> None:
    run(["git", "add", "--", *paths])
    run(["git", "commit", "-m", message])


def annotate_tag(name: str, message: str) -> None:
    run(["git", "tag", "-a", name, "-m", message])


def delete_local_tag(name: str) -> None:
    run(["git", "tag", "-d", name], check=False)


def reset_hard(sha: str) -> None:
    run(["git", "reset", "--hard", sha])


def push_atomic(tags: list[str]) -> None:
    args = ["git", "push", "--atomic", "origin", "HEAD", *tags]
    try:
        run(args)
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or "").strip()
        raise GitError(err or "git push --atomic failed") from exc
