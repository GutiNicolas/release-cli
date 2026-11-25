"""The packaging helpers every build connector shares. The go-build tests cover the tar.gz and checkout paths; these
cover what only the Windows packaging uses: the zip writer and the `binary` manifest kind."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from release_cli import packaging

EXE = b"MZ-not-a-real-exe" * 100
FILES = [("LICENSE", b"MIT\n", 0o644), ("app.exe", EXE, 0o755), ("README.md", b"# app\n", 0o644)]


def test_zip_is_reproducible_and_keeps_the_executable_bit(tmp_path: Path) -> None:
    first, second, shuffled = tmp_path / "a.zip", tmp_path / "b.zip", tmp_path / "c.zip"
    packaging.write_zip(first, FILES)
    packaging.write_zip(second, FILES)
    packaging.write_zip(shuffled, list(reversed(FILES)))
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    assert hashlib.sha256(second.read_bytes()).hexdigest() == digest
    assert hashlib.sha256(shuffled.read_bytes()).hexdigest() == digest  # the input order does not matter
    with zipfile.ZipFile(first) as zf:
        infos = zf.infolist()
        assert [i.filename for i in infos] == ["LICENSE", "README.md", "app.exe"]
        assert {i.date_time for i in infos} == {(1980, 1, 1, 0, 0, 0)}
        assert {i.filename: (i.external_attr >> 16) & 0o777 for i in infos} == {"LICENSE": 0o644, "README.md": 0o644, "app.exe": 0o755}
        assert zf.read("app.exe") == EXE and zf.testzip() is None


def test_checksums_and_manifest_list_the_zip_and_the_bare_binary(tmp_path: Path) -> None:
    stage, out = tmp_path / "stage", tmp_path / "out"
    stage.mkdir()
    (stage / "app_1.0.0_windows_amd64.exe").write_bytes(EXE)
    packaging.write_zip(stage / "app_1.0.0_windows_amd64.zip", FILES)
    artifacts = [
        packaging.entry(stage / "app_1.0.0_windows_amd64.zip", "archive", "windows", "amd64"),
        packaging.entry(stage / "app_1.0.0_windows_amd64.exe", "binary", "windows", "amd64"),
    ]
    manifest = packaging.finish(stage, out, artifacts, app="app", version="1.0.0", tag="v1.0.0", sha="a" * 40, build={"profile": "release"})

    assert json.loads((out / "manifest.json").read_text()) == manifest
    kinds = {a["name"]: (a["kind"], a.get("os"), a.get("arch")) for a in manifest["artifacts"]}
    assert kinds == {
        "app_1.0.0_windows_amd64.exe": ("binary", "windows", "amd64"),
        "app_1.0.0_windows_amd64.zip": ("archive", "windows", "amd64"),
        "checksums.txt": ("checksums", None, None),
    }
    listed = [line.split("  ")[1] for line in (out / "checksums.txt").read_text().splitlines()]
    assert listed == ["app_1.0.0_windows_amd64.exe", "app_1.0.0_windows_amd64.zip"]  # both, not checksums.txt itself
    if shutil.which("shasum"):
        check = subprocess.run(["shasum", "-a", "256", "-c", "checksums.txt"], cwd=out, capture_output=True, text=True)
        assert check.returncode == 0, check.stdout + check.stderr


def test_nothing_reaches_the_output_folder_before_finish(tmp_path: Path) -> None:
    out = tmp_path / "out"
    stage = tmp_path / "stage"
    stage.mkdir()
    packaging.write_zip(stage / "app.zip", FILES)
    assert not out.exists()  # building into the stage leaves the output folder alone
    packaging.finish(stage, out, [packaging.entry(stage / "app.zip", "archive", "windows", "amd64")], app="app", version="1", tag="v1", sha="b" * 40, build={})
    assert sorted(p.name for p in out.iterdir()) == ["app.zip", "checksums.txt", "manifest.json"]


def test_a_dry_run_without_out_dir_is_refused(tmp_path: Path) -> None:
    with pytest.raises(packaging.BuildError, match="out_dir"):
        packaging.output_dir(tmp_path, {"dry_run": True, "release_version": "1"})
