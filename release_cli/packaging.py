"""What every build connector (`go-build`, `cargo-build`) shares: the layout `sign`, `github-release` and
`homebrew-tap` read.

  * `checkout`: a temporary `git worktree` at the commit, so local changes never get in a build;
  * `output_dir`: `dist/<version>/` (git-ignored, emptied) for a real run, the CLI's `out_dir` for a dry run;
  * `write_tar_gz`, `write_zip`: reproducible archives (fixed times, owners and modes: the same commit gives the same
    sha256);
  * `entry`, `finish`: `checksums.txt` and `manifest.json`, written to a staging folder and moved to the output folder
    only when every target built, so a failure leaves no partial manifest.

Manifest kinds written here: `archive` (tar.gz or zip) and `binary` (a bare executable, with `os` and `arch`) are both
listed in `checksums.txt`; `checksums` is `checksums.txt` itself. A connector adds its own kinds (`installer`) through
`finish(extras=...)`.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ERROR_TAIL = 15
CHECKSUMMED_KINDS = ("archive", "binary")
ZIP_DATE = (1980, 1, 1, 0, 0, 0)  # the earliest time a zip can hold
REGULAR_FILE = 0o100000


class BuildError(RuntimeError):
    pass


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


def tail(text: str) -> str:
    """The last lines of a tool's output, enough to see why it failed."""
    return "\n".join(text.strip().splitlines()[-ERROR_TAIL:])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def checkout(root: Path, sha: str, prefix: str) -> Iterator[tuple[Path, Path]]:
    """Check `sha` out in a temporary worktree; yields `(work, src)`: a scratch folder and the source inside it.
    The worktree is removed on the way out, whatever happens."""
    with tempfile.TemporaryDirectory(prefix=f"{prefix}-") as tmp:
        work = Path(tmp)
        src = work / "src"
        added = git(root, "worktree", "add", "--detach", str(src), sha)
        if added.returncode != 0:
            raise BuildError(f"could not check out {sha} to build it: {tail(added.stderr)}")
        try:
            yield work, src
        finally:
            git(root, "worktree", "remove", "--force", str(src))
            git(root, "worktree", "prune")


def output_dir(root: Path, req: dict[str, Any]) -> Path:
    """Where the release files go. Dry run: the `out_dir` the CLI hands over. Real run: `dist/<version>/` in the repo,
    which must be git-ignored (the artifacts must never be committed); a previous build there is removed."""
    if req.get("dry_run"):
        if not req.get("out_dir"):
            raise BuildError("dry run without an out_dir to write into")
        return Path(req["out_dir"])
    version = req["release_version"]
    out_dir = root / "dist" / version
    if git(root, "check-ignore", "-q", "--", f"dist/{version}/manifest.json").returncode != 0:
        raise BuildError("`dist/` is not git-ignored; add `dist/` to .gitignore (the artifacts must never be committed), then resume")
    shutil.rmtree(out_dir, ignore_errors=True)
    return out_dir


def collect_files(src: Path, rel_paths: list[str]) -> list[tuple[str, bytes, int]]:
    """The extra files (licence, readme) that go in every archive: the ones that exist inside `src`, by base name."""
    root = src.resolve()
    found = []
    for rel in rel_paths:
        path = (src / rel).resolve()
        if root in path.parents and path.is_file():
            found.append((path.name, path.read_bytes(), 0o644))
    return found


def write_tar_gz(dest: Path, files: list[tuple[str, bytes, int]]) -> None:
    """A reproducible tar.gz of `(name, data, mode)` entries in the order given: fixed times and owners."""
    with dest.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
            for name, data, mode in files:
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), mode, 0
                tar.addfile(info, io.BytesIO(data))


def write_zip(dest: Path, files: list[tuple[str, bytes, int]]) -> None:
    """A reproducible zip of `(name, data, mode)` entries: sorted by name, dated 1980-01-01, the mode kept as the unix
    permission bits (0o755 for the executable, 0o644 for the rest), the same host system on every machine."""
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, data, mode in sorted(files, key=lambda f: f[0]):
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (REGULAR_FILE | mode) << 16
            zf.writestr(info, data)


def entry(path: Path, kind: str, os_name: str | None = None, arch: str | None = None) -> dict[str, Any]:
    """A manifest entry for a file. `archive` and `binary` entries carry `os` and `arch`."""
    item: dict[str, Any] = {"name": path.name, "kind": kind}
    if os_name is not None and arch is not None:
        item["os"], item["arch"] = os_name, arch
    item["sha256"], item["size"] = sha256(path), path.stat().st_size
    return item


def finish(
    stage: Path,
    out_dir: Path,
    artifacts: list[dict[str, Any]],
    *,
    app: str,
    version: str,
    tag: str,
    sha: str,
    build: dict[str, Any],
    extras: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write `checksums.txt` (every `archive` and `binary` of `artifacts`, sorted by name) and `manifest.json` into
    `stage`, then copy everything to `out_dir`. `artifacts` are the files already in `stage`; `extras` are further
    entries (an installer) that follow `checksums.txt` in the manifest. Returns the manifest."""
    listed = sorted(artifacts, key=lambda a: a["name"])
    checksums = stage / "checksums.txt"
    checksums.write_text("".join(f"{a['sha256']}  {a['name']}\n" for a in listed if a["kind"] in CHECKSUMMED_KINDS), encoding="utf-8")
    entries = [*listed, entry(checksums, "checksums"), *(extras or [])]
    manifest = {"app": app, "version": version, "tag": tag, "commit": sha, "dir": str(out_dir), "artifacts": entries, "build": build}
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # every target built: only now does anything reach the output folder
    out_dir.mkdir(parents=True, exist_ok=True)
    for item in sorted(stage.iterdir()):
        shutil.copy2(item, out_dir / item.name)
    return manifest
