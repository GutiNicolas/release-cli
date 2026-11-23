"""Built-in connector `github-release`: create the GitHub Release for the tag, with notes from the commits since the previous tag.

Uses the `gh` CLI (installed and authenticated by the user). Idempotent: when the release
already exists (resume after a failure) it is reported, not recreated.

When `go-build` finished ok, the files of its manifest (archives, checksums, installer, formula;
not the npm package) are uploaded to the release; a resume uploads only the ones still missing.
Under `release --dry-run` it calls no `gh` and only lists what it would upload.

    python -m release_cli.github_release questions|run   (protocol 1 on stdin/stdout)
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from release_cli.tagformat import TagScheme, scheme_of, tag_of
from release_cli.versioning import ProjectVersion, parse

TOOL_COMMIT_PREFIX = "[manual-release]"
GITHUB_REMOTE = re.compile(r"^(?:https?://(?:[^@/]+@)?github\.com/|ssh://git@github\.com(?::\d+)?/|git@github\.com:)[^/\s]+/[^/\s]+?(?:\.git)?/?$")
GH_MISSING = "the GitHub CLI `gh` is not on PATH; install it (https://cli.github.com) and run `gh auth login`"


class NotesError(RuntimeError):
    pass


class AssetError(RuntimeError):
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


def _manifest(req: dict[str, Any]) -> dict[str, Any] | None:
    """The `go-build` manifest when that connector finished ok, else None (the release carries no assets)."""
    entry = (req.get("prior") or {}).get("go-build") or {}
    result = entry.get("result")
    if entry.get("status") != "ok" or not isinstance(result, dict) or not isinstance(result.get("artifacts"), list):
        return None
    return result


def _assets(manifest: dict[str, Any]) -> list[tuple[str, Path, int | None]]:
    """(name, file, expected size) of every manifest file that belongs on the GitHub Release (all but the npm tgz)."""
    out_dir = Path(manifest.get("dir") or "")
    found = []
    for item in manifest["artifacts"]:
        if not isinstance(item, dict) or not item.get("name") or item.get("kind") == "npm":
            continue
        path = out_dir / item["name"]
        if not path.is_file():
            raise AssetError(f"asset {item['name']} listed in the go-build manifest is missing from {out_dir}")
        found.append((item["name"], path, item.get("size")))
    return found


def is_github_remote(url: Any) -> bool:
    return isinstance(url, str) and bool(GITHUB_REMOTE.match(url.strip()))


def questions(req: dict[str, Any]) -> dict[str, Any]:
    if not is_github_remote(req.get("remote_url")):
        return {"protocol": 1, "applies": False, "reason": "the remote is not on github.com", "questions": []}
    return {
        "protocol": 1,
        "dry_run": "safe",
        "questions": [
            {"id": "publish", "type": "bool", "prompt": f"Create the GitHub Release for {tag_of(req)}?", "default": True},
        ],
    }


def _fail(error: str, **result: Any) -> dict[str, Any]:
    return {"protocol": 1, "ok": False, "error": error, "result": result}


def _uploaded(call: Callable[..., subprocess.CompletedProcess[str]], tag: str) -> dict[str, str | None]:
    """name -> sha256 hex of the assets already on the release (None when gh gives no digest for it); empty when gh cannot say.

    The size is never trusted: a rebuilt archive can have the same size and other bytes."""
    proc = call("release", "view", tag, "--json", "assets")
    try:
        if proc.returncode != 0:
            return {}
        out: dict[str, str | None] = {}
        for a in json.loads(proc.stdout)["assets"]:
            digest = a.get("digest")
            out[a["name"]] = digest.removeprefix("sha256:").lower() if isinstance(digest, str) and digest.startswith("sha256:") else None
        return out
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dry_run(req: dict[str, Any]) -> dict[str, Any]:
    tag = tag_of(req)
    manifest = _manifest(req)
    try:
        names = [name for name, _, _ in _assets(manifest)] if manifest else []
    except AssetError as exc:
        return _fail(str(exc), published=False, dry_run=True)
    print(f"dry run: would create the GitHub Release {tag} and upload {len(names)} assets", file=sys.stderr)
    for name in names:
        print(f"  asset {name}", file=sys.stderr)
    return {"protocol": 1, "ok": True, "result": {"published": False, "dry_run": True, "assets": names}}


def run(req: dict[str, Any]) -> dict[str, Any]:
    if not (req.get("answers") or {}).get("publish"):
        return {"protocol": 1, "ok": True, "result": {"published": False}}
    if req.get("dry_run"):
        return _dry_run(req)
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
        manifest = _manifest(req)
        assets = _assets(manifest) if manifest else []
        existing = call("release", "view", tag, "--json", "url", "--jq", ".url")
        if existing.returncode == 0:
            result: dict[str, Any] = {"published": True, "existed": True, "url": existing.stdout.strip()}
        else:
            notes = release_notes(root, tag, scheme)
            with tempfile.TemporaryDirectory(prefix="release-notes-") as tmp:
                notes_file = Path(tmp) / "notes.md"
                notes_file.write_text(notes, encoding="utf-8")
                args = ["release", "create", tag, "--title", tag, "--notes-file", str(notes_file), "--verify-tag"]
                if prerelease:
                    args.append("--prerelease")
                created = call(*args)
            if created.returncode != 0:
                return _fail((created.stderr or created.stdout).strip() or f"gh release create exited {created.returncode}", published=False)
            url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
            print(f"GitHub Release created: {url}", file=sys.stderr)
            result = {"published": True, "url": url, "prerelease": prerelease}
        if manifest:
            have = _uploaded(call, tag) if existing.returncode == 0 else {}
            todo = [(name, path) for name, path, _ in assets if have.get(name) is None or have[name] != _file_sha256(path)]
            for name, path in todo:
                uploaded = call("release", "upload", tag, str(path), "--clobber")
                if uploaded.returncode != 0:
                    detail = (uploaded.stderr or uploaded.stdout).strip() or f"gh release upload exited {uploaded.returncode}"
                    return _fail(f"uploading {name} failed: {detail}", **result)
                print(f"uploaded {name}", file=sys.stderr)
            result["assets"] = [name for name, _, _ in assets]
            result["uploaded"] = [name for name, _ in todo]
    except AssetError as exc:
        return _fail(str(exc), published=False)
    except NotesError as exc:
        return _fail(f"release notes failed: {exc}", published=False)
    except OSError as exc:
        return _fail(f"could not run gh: {exc}", published=False)
    return {"protocol": 1, "ok": True, "result": result}


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
