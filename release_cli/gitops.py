"""Git helpers. All subprocesses go through `run` so tests can mock them; failures raise GitError."""

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
    _out(["git", "add", "--", *paths])
    _out(["git", "commit", "-m", message])


def annotate_tag(name: str, message: str) -> None:
    _out(["git", "tag", "-a", name, "-m", message])


def delete_local_tag(name: str) -> None:
    run(["git", "tag", "-d", name], check=False)


def reset_hard(sha: str) -> None:
    _out(["git", "reset", "--hard", sha])


def tag_commit(name: str) -> str:
    return _out(["git", "rev-parse", f"refs/tags/{name}^{{commit}}"])


def fetch_tag(name: str) -> None:
    _out(["git", "fetch", "origin", f"refs/tags/{name}:refs/tags/{name}"])


def remote_url() -> str:
    result = run(["git", "remote", "get-url", "origin"], check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def fetch_branch(branch: str) -> None:
    """Update origin/<branch>. A branch that origin does not have yet is fine (the push creates it)."""
    probe = run(["git", "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"], check=False)
    if probe.returncode == 2:  # origin is reachable and has no such branch
        return
    if probe.returncode != 0:
        raise GitError((probe.stderr or "git ls-remote origin failed").strip())
    _out(["git", "fetch", "--quiet", "--no-tags", "origin", branch])


def behind(branch: str) -> int:
    """Commits on origin/<branch> that HEAD does not have (0 when origin has no such branch)."""
    ref = f"refs/remotes/origin/{branch}"
    if run(["git", "rev-parse", "--verify", "--quiet", ref], check=False).returncode != 0:
        return 0
    return int(_out(["git", "rev-list", "--count", f"HEAD..{ref}"]))


def ahead(branch: str) -> int:
    """Commits on HEAD that origin/<branch> does not have (all of them when origin has no such branch)."""
    ref = f"refs/remotes/origin/{branch}"
    if run(["git", "rev-parse", "--verify", "--quiet", ref], check=False).returncode != 0:
        return int(_out(["git", "rev-list", "--count", "HEAD"]))
    return int(_out(["git", "rev-list", "--count", f"{ref}..HEAD"]))


def tags() -> list[str]:
    return _out(["git", "tag", "--list"]).split()


def delete_remote_tags(names: list[str]) -> None:
    _out(["git", "push", "--atomic", "origin", "--delete", *(f"refs/tags/{name}" for name in names)])


def push_atomic(tags: list[str]) -> None:
    _out(["git", "push", "--atomic", "origin", "HEAD", *tags])
