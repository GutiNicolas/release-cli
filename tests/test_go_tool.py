"""The `go` tool: init on a go.mod project, a `VERSION` file as the version, `v` tags. Real git, bare remote."""

from __future__ import annotations

from pathlib import Path

import pytest

from release_cli import adapters
from release_cli.adapters.base import AdapterError
from release_cli.cli import main
from release_cli.config import load
from release_cli.detect import detect
from tests.test_tag_boundary import git, local_tags, remote_tags

GO_MOD = "module github.com/acme/localci\n\ngo 1.22\n"


@pytest.fixture()
def go_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote, work = tmp_path / "localci.git", tmp_path / "work"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", str(work))
    (work / "go.mod").write_text(GO_MOD, encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    monkeypatch.chdir(work)
    return work


def init_and_commit(work: Path) -> None:
    main(["--init", "--defaults"])
    git(work, "add", ".")
    git(work, "commit", "-m", "Add release config and VERSION")
    git(work, "push", "origin", "main")


def test_init_on_a_go_module_writes_the_go_config_and_the_version_file(go_repo: Path) -> None:
    assert detect(go_repo) == ["go"]
    main(["--init", "--defaults"])
    cfg = load(go_repo)
    assert cfg is not None
    assert (cfg.tool, cfg.artifact, cfg.version_file, cfg.tag_format) == ("go", "localci", "VERSION", ("v{version}",))
    assert (go_repo / "VERSION").read_text(encoding="utf-8") == "0.0.0-SNAPSHOT\n"


def test_init_keeps_an_existing_version_file_and_names_a_v2_module_by_its_path_before_the_suffix(go_repo: Path) -> None:
    (go_repo / "go.mod").write_text("module github.com/acme/tools/v2\n", encoding="utf-8")
    (go_repo / "VERSION").write_text("2.3.0-SNAPSHOT\n", encoding="utf-8")
    main(["--init", "--defaults"])
    assert (go_repo / "VERSION").read_text(encoding="utf-8") == "2.3.0-SNAPSHOT\n"
    cfg = load(go_repo)
    assert cfg is not None and cfg.artifact == "tools"


def test_rc_rc_final_tag_with_v_and_leave_the_next_snapshot_in_version(go_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    init_and_commit(go_repo)

    capsys.readouterr()
    main(["--dry-run", "-rc", "--defaults"])
    out = capsys.readouterr().out
    assert "--- VERSION (release) ---" in out and "- 1: 0.0.0-SNAPSHOT" in out and "+ 1: 0.1.0-rc0" in out
    assert (go_repo / "VERSION").read_text(encoding="utf-8") == "0.0.0-SNAPSHOT\n"
    assert remote_tags(go_repo) == set()

    main(["-rc", "--defaults", "--skip-connectors"])
    (go_repo / "feature.txt").write_text("x\n", encoding="utf-8")
    git(go_repo, "add", ".")
    git(go_repo, "commit", "-m", "Add the feature")
    main(["-rc", "--defaults", "--skip-connectors"])
    main(["-fv", "--defaults", "--skip-connectors"])

    assert remote_tags(go_repo) == {"v0.1.0-rc0", "v0.1.0-rc1", "v0.1.0"} == local_tags(go_repo)
    assert (go_repo / "VERSION").read_text(encoding="utf-8") == "0.1.0-SNAPSHOT\n"
    assert git(go_repo, "show", "v0.1.0:VERSION") == "0.1.0"
    assert git(go_repo, "status", "--porcelain") == ""


def test_hotfix_from_a_v_tag_continues_as_a_patch_candidate(go_repo: Path) -> None:
    init_and_commit(go_repo)
    main(["-rc", "--defaults", "--skip-connectors"])
    main(["-fv", "--defaults", "--skip-connectors"])

    main(["hotfix", "v0.1.0"])
    assert git(go_repo, "show", "HEAD:VERSION") == "0.1.0"
    main(["-rc", "--defaults", "--skip-connectors"])
    assert "v0.1.1-rc0" in remote_tags(go_repo)


def test_a_failing_before_hook_leaves_version_untouched_and_no_tag_anywhere(go_repo: Path) -> None:
    init_and_commit(go_repo)
    cfg = (go_repo / ".release").read_text(encoding="utf-8")
    (go_repo / ".release").write_text(cfg.replace("hooks = []", '[[hooks]]\nwhen = "before"\ncmd = "false"'), encoding="utf-8")
    head = git(go_repo, "rev-parse", "HEAD")
    with pytest.raises(SystemExit):
        main(["-rc", "--defaults"])
    assert (go_repo / "VERSION").read_text(encoding="utf-8") == "0.0.0-SNAPSHOT\n"
    assert remote_tags(go_repo) == set() and local_tags(go_repo) == set()
    assert git(go_repo, "rev-parse", "HEAD") == head and git(go_repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("content", ["v0.1.0\n", "0.1\n", "0.1.0\n0.2.0\n", "version = 0.1.0\n", "\n", ""])
def test_anything_but_one_version_line_is_refused_with_what_was_read(go_repo: Path, content: str) -> None:
    init_and_commit(go_repo)
    (go_repo / "VERSION").write_text(content, encoding="utf-8")
    cfg = load(go_repo)
    assert cfg is not None
    with pytest.raises(AdapterError, match="VERSION must hold one line"):
        adapters.get("go").read(go_repo, cfg)
    with pytest.raises(AdapterError, match=repr(content.splitlines()[0] if content.strip() else "")):
        adapters.get("go").write(go_repo, cfg, "0.1.0")


def test_a_file_without_a_trailing_newline_stays_that_way(go_repo: Path) -> None:
    init_and_commit(go_repo)
    (go_repo / "VERSION").write_text("0.1.0-SNAPSHOT", encoding="utf-8")
    cfg = load(go_repo)
    assert cfg is not None
    adapters.get("go").write(go_repo, cfg, "0.1.0-rc0")
    assert (go_repo / "VERSION").read_text(encoding="utf-8") == "0.1.0-rc0"


def test_doctor_fails_on_an_unreadable_version_file_and_names_what_it_read(go_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    init_and_commit(go_repo)
    (go_repo / "VERSION").write_text("v0.1.0\n", encoding="utf-8")
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["doctor"])
    out = capsys.readouterr().out
    assert "FAIL  project version" in out and "read 'v0.1.0'" in out
