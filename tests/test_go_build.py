from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from release_cli import go_build
from release_cli.connectors import parse_questions, parse_run

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="needs the go toolchain")

FIXTURE = Path(__file__).parent / "fixtures" / "go-app"
VERSION = "0.1.0-rc0"
TAG = "v0.1.0-rc0"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = T\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "app"
    shutil.copytree(FIXTURE, root)
    _git(root, "init")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "release")
    _git(root, "tag", "-a", TAG, "-m", "rc")
    return root


def _req(repo: Path, **over: Any) -> dict[str, Any]:
    over.setdefault("sha", _git(repo, "rev-parse", "HEAD") if (repo / ".git").exists() else "HEAD")
    req = {
        "protocol": 1,
        "action": "run",
        "repo_root": str(repo),
        "artifact": "hello",
        "release_version": VERSION,
        "tag": TAG,
        "tags": [TAG],
        "tag_format": ["v{version}"],
        "mode": "rc",
        "dry_run": False,
        "out_dir": None,
        "prior": {},
        "config": {"main": "./cmd/hello"},
    }
    return {**req, **over}


def _dry(repo: Path, tmp_path: Path, **over: Any) -> tuple[dict[str, Any], Path]:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return go_build.run(_req(repo, dry_run=True, out_dir=str(out), **over)), out


def _host() -> tuple[str, str]:
    env = subprocess.run(["go", "env", "GOHOSTOS", "GOHOSTARCH"], capture_output=True, text=True, check=True).stdout.split()
    return env[0], env[1]


def test_dry_run_builds_archives_checksums_and_manifest(repo: Path, tmp_path: Path) -> None:
    resp, out = _dry(repo, tmp_path)
    outcome = parse_run(resp)
    assert outcome.status == "ok", outcome.error
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest == outcome.result
    assert (manifest["app"], manifest["version"], manifest["tag"], manifest["dir"]) == ("hello", VERSION, TAG, str(out))
    assert manifest["commit"] == _git(repo, "rev-parse", "HEAD")
    archives = [a for a in manifest["artifacts"] if a["kind"] == "archive"]
    assert [(a["os"], a["arch"]) for a in archives] == [("darwin", "amd64"), ("darwin", "arm64"), ("linux", "amd64"), ("linux", "arm64")]
    assert [a["name"] for a in archives] == [f"hello_{VERSION}_{a['os']}_{a['arch']}.tar.gz" for a in archives]
    assert manifest["artifacts"][-1]["name"] == "checksums.txt" and manifest["artifacts"][-1]["kind"] == "checksums"
    for a in manifest["artifacts"]:
        assert (out / a["name"]).stat().st_size == a["size"]
    with tarfile.open(out / archives[0]["name"]) as tar:
        assert sorted(tar.getnames()) == ["LICENSE", "README.md", "hello"]
    lines = (out / "checksums.txt").read_text().splitlines()
    assert [ln.split("  ")[1] for ln in lines] == sorted(a["name"] for a in archives)
    if shutil.which("shasum"):
        check = subprocess.run(["shasum", "-a", "256", "-c", "checksums.txt"], cwd=out, capture_output=True, text=True)
        assert check.returncode == 0, check.stdout + check.stderr
    # the temporary worktree is gone
    assert "worktree" not in _git(repo, "worktree", "list").replace(str(repo), "").replace("main", "")


def test_binary_carries_planned_version_and_embedded_file(repo: Path, tmp_path: Path) -> None:
    resp, out = _dry(repo, tmp_path)
    assert resp["ok"], resp
    goos, goarch = _host()
    name = f"hello_{VERSION}_{goos}_{goarch}.tar.gz"
    if not (out / name).exists():
        pytest.skip(f"host {goos}/{goarch} is not a default target")
    unpacked = tmp_path / "unpacked"
    with tarfile.open(out / name) as tar:
        tar.extractall(unpacked, filter="data")
    assert subprocess.run([str(unpacked / "hello"), "version"], capture_output=True, text=True).stdout.strip() == VERSION
    assert subprocess.run([str(unpacked / "hello")], capture_output=True, text=True).stdout == (FIXTURE / "cmd/hello/greeting.txt").read_text()


def test_real_run_builds_the_tag_not_the_dirty_tree(repo: Path, tmp_path: Path) -> None:
    clean = go_build.run(_req(repo))
    assert clean["ok"], clean
    dist = repo / "dist" / VERSION
    before = (dist / "checksums.txt").read_text()
    # local edits (tracked and untracked) must not reach the build
    (repo / "cmd/hello/greeting.txt").write_text("not committed\n")
    (repo / "cmd/hello/extra.go").write_text("package main\nthis does not compile\n")
    dirty = go_build.run(_req(repo))
    assert dirty["ok"], dirty
    assert (dist / "checksums.txt").read_text() == before
    assert json.loads((dist / "manifest.json").read_text())["dir"] == str(dist)


def test_real_run_refuses_when_dist_is_not_ignored(repo: Path) -> None:
    (repo / ".gitignore").write_text("")
    _git(repo, "commit", "-am", "no ignore")
    resp = go_build.run(_req(repo, sha=_git(repo, "rev-parse", "HEAD")))
    assert resp["ok"] is False and "git-ignored" in resp["error"]
    assert not (repo / "dist").exists()


def test_missing_go_fails_with_install_hint(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    req = _req(repo)
    monkeypatch.setenv("PATH", str(empty))
    resp = go_build.run(req)
    assert resp["ok"] is False and "https://go.dev/dl" in resp["error"]


def test_a_target_that_does_not_compile_fails_without_a_manifest(repo: Path, tmp_path: Path) -> None:
    # the _linux suffix makes this file part of the linux builds only: darwin compiles, linux does not
    (repo / "cmd/hello/broken_linux.go").write_text('package main\n\nvar _ int = "not an int"\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "break linux")
    resp, out = _dry(repo, tmp_path, sha=_git(repo, "rev-parse", "HEAD"))
    assert resp["ok"] is False
    assert "linux/amd64" in resp["error"] and "broken_linux.go" in resp["error"]
    assert list(out.iterdir()) == []  # not even the darwin archives that did build


def test_questions_declare_dry_run_safe_and_skip_non_go_repos(repo: Path, tmp_path: Path) -> None:
    qs = parse_questions(go_build.questions(_req(repo)))
    assert qs.applies and qs.dry_run_safe
    other = tmp_path / "maven-app"
    other.mkdir()
    assert parse_questions(go_build.questions(_req(other))).applies is False
