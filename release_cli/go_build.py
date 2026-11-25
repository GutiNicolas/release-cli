"""Built-in connector `go-build`: cross-compile a Go app with native `go build` and package the result.

For every target it builds the pushed commit (a temporary `git worktree`, so local changes never get in), stamps the
version with ldflags and writes `<binary>_<version>_<os>_<arch>.tar.gz`, `checksums.txt` and `manifest.json`.
Real run: `dist/<version>/` in the repo (it must be git-ignored). Dry run: the `out_dir` the CLI hands over; the
commit built is then HEAD. Nothing is uploaded or published here.

    python -m release_cli.go_build questions|run   (protocol 1 on stdin/stdout)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from release_cli import packaging
from release_cli.packaging import BuildError
from release_cli.tagformat import tag_of

DEFAULT_TARGETS = ["darwin/amd64", "darwin/arm64", "linux/amd64", "linux/arm64"]
DEFAULT_LDFLAGS = "-s -w -X main.version={version}"
TARGET_RE = re.compile(r"^[a-z0-9]+/[a-z0-9]+$")
GO_MISSING = "`go` is not on PATH; install Go (https://go.dev/dl) and make sure `go version` works in this shell"
GITHUB_SLUG = re.compile(r"^(?:https?://(?:[^@/]+@)?github\.com/|ssh://git@github\.com(?::\d+)?/|git@github\.com:)([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
INSTALLER = Path(__file__).parent / "templates" / "install.sh"


def questions(req: dict[str, Any]) -> dict[str, Any]:
    if not (Path(req["repo_root"]) / "go.mod").is_file():
        return {"protocol": 1, "applies": False, "reason": "no go.mod at the repo root", "questions": []}
    return {"protocol": 1, "dry_run": "safe", "questions": []}


def _settings(req: dict[str, Any]) -> dict[str, Any]:
    cfg = req.get("config") or {}
    binary = cfg.get("binary") or req.get("artifact")
    targets = cfg.get("targets", DEFAULT_TARGETS)
    files = cfg.get("archive_files", ["LICENSE", "README.md"])
    main = cfg.get("main", ".")
    ldflags = cfg.get("ldflags", DEFAULT_LDFLAGS)
    cgo = cfg.get("cgo", False)
    if not isinstance(binary, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", binary):
        raise BuildError("set `binary` under [connectors.go-build] (or `artifact`): letters, digits, `.`, `_` and `-` only")
    if not isinstance(main, str) or not main:
        raise BuildError("`main` must be the package to build, for example \"./cmd/app\"")
    if not isinstance(targets, list) or not targets or not all(isinstance(t, str) and TARGET_RE.match(t) for t in targets):
        raise BuildError("`targets` must be a list of \"os/arch\" strings, for example [\"linux/amd64\"]")
    if not isinstance(ldflags, str):
        raise BuildError("`ldflags` must be text")
    if not isinstance(cgo, bool):
        raise BuildError("`cgo` must be true or false")
    if not isinstance(files, list) or not all(isinstance(f, str) and f for f in files):
        raise BuildError("`archive_files` must be a list of file paths")
    return {"binary": binary, "main": main, "targets": list(dict.fromkeys(targets)), "ldflags": ldflags, "cgo": cgo, "files": files}


def github_slug(remote_url: Any) -> tuple[str, str] | None:
    """(owner, repo) of a github.com remote URL, or None for any other remote."""
    match = GITHUB_SLUG.match(remote_url.strip()) if isinstance(remote_url, str) else None
    return (match.group(1), match.group(2)) if match else None


def download_base(req: dict[str, Any]) -> str | None:
    """Where the GitHub Release of this tag serves its files; None when the remote is not on github.com."""
    slug = github_slug(req.get("remote_url"))
    return f"https://github.com/{slug[0]}/{slug[1]}/releases/download/{tag_of(req)}" if slug else None


def _render_installer(dest: Path, base: str, binary: str, version: str, tag: str) -> None:
    values = {
        "@BINARY@": binary,
        "@VERSION@": version,
        "@TAG_TEMPLATE@": tag.replace(version, "{version}"),
        "@BASE_URL@": base.rsplit("/", 1)[0],
    }
    text = INSTALLER.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace(key, value)
    dest.write_text(text, encoding="utf-8")
    dest.chmod(0o755)


def _build_target(src: Path, work: Path, spec: dict[str, Any], version: str, target: str, goos: str, goarch: str) -> Path:
    exe = spec["binary"] + (".exe" if goos == "windows" else "")
    out = work / f"bin-{goos}-{goarch}" / exe
    ldflags = spec["ldflags"].replace("{version}", version)
    cmd = ["go", "build", "-trimpath", "-o", str(out)]
    if ldflags:
        cmd += ["-ldflags", ldflags]
    cmd.append(spec["main"])
    env = {**os.environ, "GOOS": goos, "GOARCH": goarch, "CGO_ENABLED": "1" if spec["cgo"] else "0", "GOFLAGS": "-mod=readonly"}
    proc = subprocess.run(cmd, cwd=src, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise BuildError(f"go build failed for {target}: {packaging.tail(proc.stderr or proc.stdout) or f'exit {proc.returncode}'}")
    return out


def _package(src: Path, work: Path, out_dir: Path, req: dict[str, Any], spec: dict[str, Any], version: str, tag: str, sha: str) -> dict[str, Any]:
    extras = packaging.collect_files(src, spec["files"])
    stage = work / "stage"
    stage.mkdir()
    artifacts: list[dict[str, Any]] = []
    for target in spec["targets"]:
        goos, goarch = target.split("/")
        print(f"go-build: {target}", file=sys.stderr)
        binary = _build_target(src, work, spec, version, target, goos, goarch)
        name = f"{spec['binary']}_{version}_{goos}_{goarch}.tar.gz"
        packaging.write_tar_gz(stage / name, [(binary.name, binary.read_bytes(), 0o755), *extras])
        artifacts.append(packaging.entry(stage / name, "archive", goos, goarch))
    installed = []
    if base := download_base(req):
        installer = stage / "install.sh"
        _render_installer(installer, base, spec["binary"], version, tag)
        installed.append(packaging.entry(installer, "installer"))
    else:
        print("go-build: the remote is not on github.com, so install.sh is not rendered", file=sys.stderr)
    build = {"main": spec["main"], "targets": spec["targets"], "ldflags": spec["ldflags"], "cgo": spec["cgo"], "flags": ["-trimpath", "-mod=readonly"]}
    return packaging.finish(stage, out_dir, artifacts, app=req.get("artifact") or spec["binary"], version=version, tag=tag, sha=sha, build=build, extras=installed)


def _fail(error: str, **result: Any) -> dict[str, Any]:
    return {"protocol": 1, "ok": False, "error": error, "result": result}


def run(req: dict[str, Any]) -> dict[str, Any]:
    if shutil.which("go") is None:
        return _fail(GO_MISSING)
    root = Path(req["repo_root"])
    try:
        spec = _settings(req)
        out_dir = packaging.output_dir(root, req)
        with packaging.checkout(root, req["sha"], "go-build") as (work, src):
            manifest = _package(src, work, out_dir, req, spec, req["release_version"], tag_of(req), req["sha"])
    except (BuildError, OSError) as exc:
        return _fail(str(exc))
    print(f"go-build: {len(manifest['artifacts'])} files in {out_dir}", file=sys.stderr)
    return {"protocol": 1, "ok": True, "result": manifest}


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
