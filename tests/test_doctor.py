"""`release doctor`: each failure names what to do, a healthy project exits 0, and nothing is changed."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from release_cli import connectors
from release_cli.cli import main
from tests.conftest import write_config
from tests.test_connectors_contract import POM, install_fakes


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote = tmp_path / "example-app.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", str(work))
    (work / "pom.xml").write_text(POM.read_text(encoding="utf-8"), encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    write_config(work)
    monkeypatch.chdir(work)
    return work


def run_doctor(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, list[dict[str, str]]]:
    with pytest.raises(SystemExit) as exit_info:
        main(["doctor", "--json", *args])
    code = exit_info.value.code
    return int(code or 0), json.loads(capsys.readouterr().out)


def run_doctor_ok(capsys: pytest.CaptureFixture[str]) -> list[dict[str, str]]:
    main(["doctor", "--json"])  # no SystemExit: exit code 0
    return json.loads(capsys.readouterr().out)


def by_name(checks: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {c["name"]: c for c in checks}


def test_healthy_project_exits_zero(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_fakes(("platform-deploy", "ok"))
    checks = by_name(run_doctor_ok(capsys))
    assert checks["project version"]["status"] == "ok"
    assert checks["git sync"]["status"] == "ok"
    assert checks["connector platform-deploy"]["status"] == "ok"
    assert not any(c["status"] == "fail" for c in checks.values())


def test_doctor_changes_nothing(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = (git(repo, "status", "--porcelain"), git(repo, "rev-parse", "origin/main"))
    run_doctor_ok(capsys)
    assert (git(repo, "status", "--porcelain"), git(repo, "rev-parse", "origin/main")) == before
    assert not connectors.global_path().exists()


def test_external_connector_without_environment_fails_with_the_fix(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    connectors.save_global({"order": ["jira"], "connectors": {"jira": {"entry_point": "jira", "enabled": True}}})
    code, checks = run_doctor(capsys)
    check = by_name(checks)["connector jira"]
    assert code == 1
    assert check["status"] == "fail"
    assert check["fix"] == "release connector update jira"


def test_external_connector_whose_entry_point_is_gone_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    connectors.env_python().parent.mkdir(parents=True)
    connectors.env_python().symlink_to(sys.executable)
    connectors.save_global({"order": ["jira"], "connectors": {"jira": {"entry_point": "jira", "enabled": True}}})
    code, checks = run_doctor(capsys)
    assert code == 1
    assert by_name(checks)["connector jira"]["status"] == "fail"


def test_not_a_git_repo_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pom.xml").write_text(POM.read_text(encoding="utf-8"), encoding="utf-8")
    write_config(plain)
    monkeypatch.chdir(plain)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    code, checks = run_doctor(capsys)
    assert code == 1
    assert by_name(checks)["git repo"]["status"] == "fail"


def test_branch_behind_origin_fails(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    other = tmp_path / "other"
    git(tmp_path, "clone", str(tmp_path / "example-app.git"), str(other))
    git(other, "commit", "--allow-empty", "-m", "someone else pushed")
    git(other, "push", "origin", "main")
    code, checks = run_doctor(capsys)
    sync = by_name(checks)["git sync"]
    assert code == 1
    assert sync["status"] == "fail"
    assert "git pull" in sync["fix"]


def test_unreadable_version_file_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "pom.xml").unlink()
    code, checks = run_doctor(capsys)
    assert code == 1
    assert by_name(checks)["project version"]["status"] == "fail"


def test_project_connector_override_naming_an_uninstalled_connector_warns(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_config(repo)
    with (repo / ".release").open("a", encoding="utf-8") as fh:
        fh.write('\n[connectors]\ndisabled = ["ghost"]\n')
    install_fakes(("platform-deploy", "ok"))
    checks = by_name(run_doctor_ok(capsys))
    assert checks["project connectors"]["status"] == "warn"
    assert "ghost" in checks["project connectors"]["message"]
