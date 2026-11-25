"""The `cargo` tool: init on a Cargo project, the version in `Cargo.toml` and `Cargo.lock` by text edits, `v` tags."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from release_cli import adapters
from release_cli.adapters.base import AdapterError
from release_cli.cli import main
from release_cli.config import load
from release_cli.detect import detect
from tests.test_tag_boundary import git, local_tags, remote_tags

FIXTURE = Path(__file__).parent / "fixtures" / "cargo-app"
SINGLE_CRATE = '[package]\nname = "acme-tool"\nversion = "0.2.0-SNAPSHOT" # keep me\nedition = "2021"\n\n[dependencies]\n'


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path | None) -> Path:
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote, work = tmp_path / "acme.git", tmp_path / "work"
    git(tmp_path, "init", "--bare", str(remote))
    if source is not None:
        shutil.copytree(source, work)
    else:
        work.mkdir()
    git(work, "init")
    return work


def _commit_and_push(work: Path, remote: Path) -> None:
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")


@pytest.fixture()
def cargo_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = _repo(tmp_path, monkeypatch, FIXTURE)
    _commit_and_push(work, tmp_path / "acme.git")
    monkeypatch.chdir(work)
    return work


def _init(work: Path, *, commit: bool = True) -> None:
    main(["--init", "--defaults"])
    if commit:
        git(work, "add", ".")
        git(work, "commit", "-m", "Add release config")
        git(work, "push", "origin", "main")


def test_init_on_a_cargo_workspace_writes_the_cargo_config_and_names_the_only_binary(cargo_repo: Path) -> None:
    assert detect(cargo_repo) == ["cargo"]
    main(["--init", "--defaults"])
    cfg = load(cargo_repo)
    assert cfg is not None
    assert (cfg.tool, cfg.artifact, cfg.version_file, cfg.tag_format) == ("cargo", "hello", "Cargo.toml", ("v{version}",))


def test_rc_bumps_toml_and_lock_changes_only_those_lines_and_tags_with_v(cargo_repo: Path) -> None:
    _init(cargo_repo)
    cfg = load(cargo_repo)
    assert cfg is not None
    adapters.get("cargo").write(cargo_repo, cfg, "0.0.0-SNAPSHOT")
    git(cargo_repo, "commit", "-am", "Start from a snapshot")
    git(cargo_repo, "push", "origin", "main")

    main(["-rc", "--defaults", "--skip-connectors"])

    assert remote_tags(cargo_repo) == {"v0.1.0-rc0"} == local_tags(cargo_repo)
    diff = git(cargo_repo, "diff", "--unified=0", "v0.1.0-rc0~1", "v0.1.0-rc0")
    changed = [line for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    assert changed == [
        '-version = "0.0.0-SNAPSHOT"',  # Cargo.lock: greet, hello
        '+version = "0.1.0-rc0"',
        '-version = "0.0.0-SNAPSHOT"',
        '+version = "0.1.0-rc0"',
        '-version = "0.0.0-SNAPSHOT" # one version for the whole repository',  # Cargo.toml, comment kept
        '+version = "0.1.0-rc0" # one version for the whole repository',
    ]
    assert "Cargo.lock" in git(cargo_repo, "show", "--stat", "--format=", "v0.1.0-rc0")
    assert 'version = "0.1.0-rc0-SNAPSHOT"' in (cargo_repo / "Cargo.lock").read_text(encoding="utf-8")
    assert git(cargo_repo, "status", "--porcelain") == ""


@pytest.mark.skipif(shutil.which("cargo") is None, reason="needs the rust toolchain")
def test_the_lock_stays_consistent_after_a_write(cargo_repo: Path) -> None:
    _init(cargo_repo, commit=False)
    cfg = load(cargo_repo)
    assert cfg is not None
    adapters.get("cargo").write(cargo_repo, cfg, "0.3.0-rc1")
    done = subprocess.run(["cargo", "metadata", "--locked", "--offline", "--format-version", "1"], cwd=cargo_repo, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_a_single_crate_without_a_lock_reads_and_writes_keeping_comments_and_crlf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = _repo(tmp_path, monkeypatch, None)
    (work / "Cargo.toml").write_bytes(SINGLE_CRATE.replace("\n", "\r\n").encode())
    _commit_and_push(work, tmp_path / "acme.git")
    monkeypatch.chdir(work)
    main(["--init", "--defaults"])
    cfg = load(work)
    assert cfg is not None and (cfg.artifact, cfg.tag_format) == ("acme-tool", ("v{version}",))
    adapter = adapters.get("cargo")
    assert adapter.read(work, cfg).version == "0.2.0-SNAPSHOT"
    assert adapter.paths(work, cfg) == [work / "Cargo.toml"]
    assert adapter.write(work, cfg, "0.2.0-rc0") == [work / "Cargo.toml"]
    assert (work / "Cargo.toml").read_bytes() == SINGLE_CRATE.replace("0.2.0-SNAPSHOT", "0.2.0-rc0").replace("\n", "\r\n").encode()


def test_a_lock_that_git_does_not_track_is_left_alone(cargo_repo: Path) -> None:
    (cargo_repo / ".gitignore").write_text("target/\nCargo.lock\n", encoding="utf-8")
    git(cargo_repo, "rm", "--cached", "-q", "Cargo.lock")
    cfg = _config(cargo_repo)
    before = (cargo_repo / "Cargo.lock").read_bytes()
    assert adapters.get("cargo").paths(cargo_repo, cfg) == [cargo_repo / "Cargo.toml"]
    assert adapters.get("cargo").write(cargo_repo, cfg, "0.1.0-rc1") == [cargo_repo / "Cargo.toml"]
    assert (cargo_repo / "Cargo.lock").read_bytes() == before


def _config(work: Path):
    main(["--init", "--defaults"])
    cfg = load(work)
    assert cfg is not None
    return cfg


def test_a_member_with_its_own_version_is_refused_naming_its_manifest(cargo_repo: Path) -> None:
    cfg = _config(cargo_repo)
    member = cargo_repo / "crates" / "greet" / "Cargo.toml"
    member.write_text(member.read_text(encoding="utf-8").replace("version.workspace = true", 'version = "0.9.0"'), encoding="utf-8")
    adapter = adapters.get("cargo")
    with pytest.raises(AdapterError, match=r"crates/greet/Cargo\.toml"):
        adapter.read(cargo_repo, cfg)
    with pytest.raises(AdapterError, match=r"crates/greet/Cargo\.toml"):
        adapter.write(cargo_repo, cfg, "0.2.0-rc0")
    assert (cargo_repo / "Cargo.toml").read_text(encoding="utf-8").count("0.1.0-rc0") == 1


@pytest.mark.parametrize("bad", ["v0.1.0", "0.1", "0.1.0-beta1", "1.0.0-rc"])
def test_a_version_that_is_not_x_y_z_with_optional_rc_is_refused(cargo_repo: Path, bad: str) -> None:
    cfg = _config(cargo_repo)
    toml = cargo_repo / "Cargo.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace("0.1.0-rc0", bad), encoding="utf-8")
    with pytest.raises(AdapterError, match=f"Cargo.toml.*{bad}"):
        adapters.get("cargo").read(cargo_repo, cfg)
