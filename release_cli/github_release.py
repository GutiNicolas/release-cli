"""Built-in connector `github-release`: create the GitHub Release for the tag, with notes from the commits since the previous tag.

Uses the `gh` CLI (installed and authenticated by the user). Idempotent: when the release
already exists (resume after a failure) it is reported, not recreated.

    python -m release_cli.github_release questions|run   (protocol 1 on stdin/stdout)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from release_cli.tagformat import TagScheme, scheme_of, tag_of
from release_cli.versioning import ProjectVersion, parse

TOOL_COMMIT_PREFIX = "[manual-release]"
GITHUB_REMOTE = re.compile(r"^(?:https?://(?:[^@/]+@)?github\.com/|ssh://git@github\.com(?::\d+)?/|git@github\.com:)[^/\s]+/[^/\s]+?(?:\.git)?/?$")
GH_MISSING = "the GitHub CLI `gh` is not on PATH; install it (https://cli.github.com) and run `gh auth login`"


class NotesError(RuntimeError):
    pass


def _key(v: ProjectVersion) -> tuple[int, int, int, float]:
    # An RC sorts before its final: 1.2.0-rc3 < 1.2.0
    return (v.major, v.minor, v.patch, float("inf") if v.rc is None else v.rc)


def _plain(tag: str, scheme: TagScheme | None = None) -> ProjectVersion | None:
    """The version of a primary-form tag (history walks one tag per release), None for anything else or a SNAPSHOT."""
    version = (scheme or TagScheme()).primary_version_of(tag)
    if version is None:
        return None
    parsed = parse(version)
    return None if parsed.snapshot else parsed


def previous_tag(tag: str, tags: list[str], scheme: TagScheme | None = None) -> str | None:
    """Greatest primary tag strictly lower than `tag`. Finals only compare against finals."""
    scheme = scheme or TagScheme()
    current = parse(scheme.version_of(tag) or tag)
    best: tuple[tuple[int, int, int, float], str] | None = None
    for name in tags:
        candidate = _plain(name, scheme)
        if candidate is None or (current.rc is None and candidate.rc is not None):
            continue
        key = _key(candidate)
        if key < _key(current) and (best is None or key > best[0]):
            best = (key, name)
    return best[1] if best else None


def latest_tag(tags: list[str], scheme: TagScheme | None = None) -> str | None:
    """Greatest primary version tag (RC or final), or None."""
    found = [(_key(v), tag) for tag in tags if (v := _plain(tag, scheme)) is not None]
    return max(found)[1] if found else None


def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise NotesError(f"could not run git: {exc}") from exc
    if proc.returncode != 0:
        raise NotesError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def commit_range(repo_root: Path, tag: str, *, end: str | None = None, scheme: TagScheme | None = None) -> tuple[str | None, str]:
    """(previous tag, git range) of the commits `tag` ships: since the previous tag (a final: the previous final)."""
    prev = previous_tag(tag, _git(repo_root, "tag", "--list").split(), scheme)
    end = end or tag
    return prev, f"{prev}..{end}" if prev else end


def git_log(repo_root: Path, rng: str, pretty: str, *, merges: bool = False) -> str:
    return _git(repo_root, "log", *([] if merges else ["--no-merges"]), f"--pretty=format:{pretty}", rng, "--")


def release_notes(repo_root: Path, tag: str, scheme: TagScheme | None = None) -> str:
    prev, rng = commit_range(repo_root, tag, scheme=scheme)
    log = git_log(repo_root, rng, "%h%x09%s")
    bullets = []
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        if subject.startswith(TOOL_COMMIT_PREFIX):
            continue
        bullets.append(f"- {subject} ({sha})")
    heading = f"## Changes since {prev}" if prev else "## Changes"
    return "\n".join([heading, "", *(bullets or ["- No changes."])]) + "\n"


def is_github_remote(url: Any) -> bool:
    return isinstance(url, str) and bool(GITHUB_REMOTE.match(url.strip()))


def questions(req: dict[str, Any]) -> dict[str, Any]:
    if not is_github_remote(req.get("remote_url")):
        return {"protocol": 1, "applies": False, "reason": "the remote is not on github.com", "questions": []}
    return {
        "protocol": 1,
        "questions": [
            {"id": "publish", "type": "bool", "prompt": f"Create the GitHub Release for {tag_of(req)}?", "default": True},
        ],
    }


def _fail(error: str, **result: Any) -> dict[str, Any]:
    return {"protocol": 1, "ok": False, "error": error, "result": result}


def run(req: dict[str, Any]) -> dict[str, Any]:
    if not (req.get("answers") or {}).get("publish"):
        return {"protocol": 1, "ok": True, "result": {"published": False}}
    gh = shutil.which("gh")
    if gh is None:
        return _fail(GH_MISSING, published=False)
    root = Path(req["repo_root"])
    tag = tag_of(req)
    scheme = scheme_of(req)
    prerelease = (version := scheme.version_of(tag)) is not None and parse(version).rc is not None

    def call(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([gh, *args], cwd=root, capture_output=True, text=True, check=False)

    try:
        existing = call("release", "view", tag, "--json", "url", "--jq", ".url")
        if existing.returncode == 0:
            return {"protocol": 1, "ok": True, "result": {"published": True, "existed": True, "url": existing.stdout.strip()}}
        notes = release_notes(root, tag, scheme)
        with tempfile.TemporaryDirectory(prefix="release-notes-") as tmp:
            notes_file = Path(tmp) / "notes.md"
            notes_file.write_text(notes, encoding="utf-8")
            args = ["release", "create", tag, "--title", tag, "--notes-file", str(notes_file), "--verify-tag"]
            if prerelease:
                args.append("--prerelease")
            created = call(*args)
    except NotesError as exc:
        return _fail(f"release notes failed: {exc}", published=False)
    except OSError as exc:
        return _fail(f"could not run gh: {exc}", published=False)
    if created.returncode != 0:
        return _fail((created.stderr or created.stdout).strip() or f"gh release create exited {created.returncode}", published=False)
    url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
    print(f"GitHub Release created: {url}", file=sys.stderr)
    return {"protocol": 1, "ok": True, "result": {"published": True, "url": url, "prerelease": prerelease}}


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    req = json.load(sys.stdin)
    action = req.get("action", action)
    if action == "questions":
        resp = questions(req)
    elif action == "run":
        resp = run(req)
    else:
        print(f"unknown action {action!r}", file=sys.stderr)
        raise SystemExit(2)
    json.dump(resp, sys.stdout)


if __name__ == "__main__":
    main()
