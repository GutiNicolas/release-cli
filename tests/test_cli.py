from __future__ import annotations

import sys
from pathlib import Path

import pytest

from release_cli.cli import _paint, build_parser, main
from release_cli.gitops import RepoState
from release_cli import gitops
from tests.conftest import write_config

FIXTURE = Path(__file__).parent / "fixtures" / "juggler-like.pom.xml"


def _maven_project(tmp_path: Path, xml: str | None = None) -> None:
    (tmp_path / "pom.xml").write_text(xml or FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    write_config(tmp_path)


def test_paint_when_stderr_is_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    painted = _paint("\033[0;32m", "STARTING RELEASE")
    assert "STARTING RELEASE" in painted
    assert painted.startswith("\033[0;32m")


def test_parser_accepts_dash_rc_as_one_flag() -> None:
    ns = build_parser().parse_args(["-rc", "--major"])
    assert ns.rc is True
    assert ns.major is True
    assert ns.fv is False


def test_dry_run_rc_does_not_write(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _maven_project(tmp_path)
    before = (tmp_path / "pom.xml").read_text(encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    main(["-rc", "--dry-run"])
    assert (tmp_path / "pom.xml").read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "1.5.0-rc0" in out
    assert "DRY-RUN" in out
    assert "+ " in out
    assert "Would you like to run" not in out


def test_fv_without_rc_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _maven_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["-fv", "--dry-run"])


def test_major_during_rc_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _maven_project(tmp_path, FIXTURE.read_text(encoding="utf-8").replace("1.4.0-SNAPSHOT", "1.4.0-rc2-SNAPSHOT"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["-rc", "--major", "--dry-run"])


def _clean_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitops, "current_repo", lambda: RepoState("abc", "main"))
    monkeypatch.setattr(gitops, "porcelain", lambda: "")
    monkeypatch.setattr(gitops, "tag_exists_local", lambda _name: False)
    monkeypatch.setattr(gitops, "remote_tag_exists", lambda _name: False)


def test_dirty_tree_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _maven_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    _clean_repo(monkeypatch)
    monkeypatch.setattr(gitops, "porcelain", lambda: " M README.md")
    with pytest.raises(SystemExit) as exc:
        main(["-rc", "--skip-hooks"])
    assert exc.value.code == 1
    assert "working tree is not clean" in capsys.readouterr().err
    assert "1.4.0-SNAPSHOT" in (tmp_path / "pom.xml").read_text(encoding="utf-8")


def test_existing_local_tag_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _maven_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    _clean_repo(monkeypatch)
    monkeypatch.setattr(gitops, "tag_exists_local", lambda _name: True)
    with pytest.raises(SystemExit) as exc:
        main(["-rc", "--skip-hooks"])
    assert exc.value.code == 1
    assert "local tag already exists" in capsys.readouterr().err


def test_first_run_without_config_stops_if_gitignore_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pom.xml").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gitops, "porcelain", lambda: "?? .gitignore")
    main(["-rc", "--dry-run"])
    out = capsys.readouterr().out
    assert "no .release" in out
    assert "not releasing" in out
    assert (tmp_path / ".release").is_file()
    assert "1.4.0-SNAPSHOT" in (tmp_path / "pom.xml").read_text(encoding="utf-8")


def test_dry_run_prints_configured_hook(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _maven_project(tmp_path)
    write_config(
        tmp_path,
        hooks='[[hooks]]\nwhen = "after"\ncmd = "mvn deploy"\n',
    )
    monkeypatch.chdir(tmp_path)
    main(["-rc", "--dry-run"])
    out = capsys.readouterr().out
    assert "HOOK after: mvn deploy" in out
    assert "mvn test" not in out


def test_before_hook_failure_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _maven_project(tmp_path)
    write_config(
        tmp_path,
        hooks='[[hooks]]\nwhen = "before"\ncmd = "false"\n',
    )
    monkeypatch.chdir(tmp_path)
    _clean_repo(monkeypatch)
    monkeypatch.setattr("release_cli.hooks.run_command", lambda _cmd: 1)
    before = (tmp_path / "pom.xml").read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["-rc", "-y"])
    assert (tmp_path / "pom.xml").read_text(encoding="utf-8") == before
