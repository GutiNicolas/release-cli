"""Real git with a --bare remote: the tag boundary and resuming a pushed tag."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from release_cli import connectors, gitops
from release_cli.cli import main
from tests.conftest import write_config
from tests.test_connectors_contract import FAKE, POM, install_fakes, run_requests

TAGS = ("1.5.0-rc0", "example-app-1.5.0-rc0")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote = tmp_path / "example-app.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", str(work))
    (work / "pom.xml").write_text(POM.read_text(encoding="utf-8"), encoding="utf-8")
    (work / ".gitignore").write_text(".release\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    write_config(work)
    monkeypatch.chdir(work)
    monkeypatch.setenv("FAKE_LOG", str(work.parent / "calls.jsonl"))
    return work


def remote_tags(work: Path) -> set[str]:
    out = git(work, "ls-remote", "--tags", "origin")
    return {line.split("refs/tags/")[1] for line in out.splitlines() if not line.endswith("^{}")}


def local_tags(work: Path) -> set[str]:
    return set(git(work, "tag").split())


def test_release_pushes_both_tags(repo: Path) -> None:
    main(["-rc", "--defaults"])
    assert remote_tags(repo) == set(TAGS)
    assert "1.5.0-rc0-SNAPSHOT" in (repo / "pom.xml").read_text(encoding="utf-8")
    assert git(repo, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")


def test_failing_hook_before_push_rolls_back(repo: Path) -> None:
    write_config(repo, hooks='[[hooks]]\nwhen = "after"\ncmd = "false"\n')
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(SystemExit):
        main(["-rc", "--defaults"])
    assert remote_tags(repo) == set()
    assert local_tags(repo) == set()
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") == ""


def test_failing_connector_after_push_keeps_tags(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    resets: list[str] = []
    deletes: list[str] = []
    monkeypatch.setattr(gitops, "reset_hard", lambda sha: resets.append(sha))
    monkeypatch.setattr(gitops, "delete_local_tag", lambda tag: deletes.append(tag))
    install_fakes(("platform-build", "fail"), ("platform-deploy", "echo"))
    with pytest.raises(SystemExit) as exc:
        main(["-rc", "--defaults"])
    assert exc.value.code == 1
    assert resets == [] and deletes == []
    assert remote_tags(repo) == set(TAGS)
    assert set(TAGS) <= local_tags(repo)
    err = capsys.readouterr()
    assert "tags stay" in err.err
    assert "release connectors run 1.5.0-rc0 --from platform-build" in err.out
    state = connectors.load_state("example-app", "1.5.0-rc0")
    assert state is not None and state["connectors"]["platform-build"]["status"] == "failed"
    assert state["sha"] == git(repo, "rev-parse", "1.5.0-rc0^{commit}")


def _swap(name: str, behavior: str) -> None:
    g = connectors.load_global()
    g["connectors"][name]["command"] = [sys.executable, FAKE, behavior]
    connectors.save_global(g)


def test_resume_from_uses_saved_results(repo: Path) -> None:
    install_fakes(("platform-build", "echo"), ("platform-deploy", "fail"))
    with pytest.raises(SystemExit):
        main(["-rc", "--defaults"])
    _swap("platform-deploy", "echo")
    main(["connectors", "run", "1.5.0-rc0", "--from", "platform-deploy", "--defaults"])
    calls = run_requests(repo.parent)
    assert [c["connector"] for c in calls] == ["platform-build", "platform-deploy", "platform-deploy"]
    assert calls[-1]["prior"]["platform-build"]["result"]["by"] == "platform-build"
    state = connectors.load_state("example-app", "1.5.0-rc0")
    assert state["connectors"]["platform-deploy"]["status"] == "ok"


def test_only_runs_one_with_other_saved_results(repo: Path) -> None:
    install_fakes(("platform-build", "echo"), ("platform-deploy", "echo"), ("cursor-review", "echo"))
    main(["-rc", "--defaults"])
    main(["connectors", "run", "example-app-1.5.0-rc0", "--only", "platform-build", "--defaults"])
    calls = run_requests(repo.parent)
    assert [c["connector"] for c in calls][3:] == ["platform-build"]
    assert sorted(calls[-1]["prior"]) == ["cursor-review", "platform-deploy"]


def test_interrupted_job_is_repolled_not_relaunched(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_fakes(("platform-build", "interrupt"))
    with pytest.raises(SystemExit):
        main(["-rc", "--defaults"])
    out = capsys.readouterr().out
    assert "job_id: job-1" in out
    state = connectors.load_state("example-app", "1.5.0-rc0")
    assert state["connectors"]["platform-build"] == {**state["connectors"]["platform-build"], "status": "interrupted", "job_id": "job-1"}
    main(["connectors", "run", "1.5.0-rc0", "--defaults"])
    calls = run_requests(repo.parent)
    assert calls[0]["resume_job_id"] is None
    assert calls[1]["resume_job_id"] == "job-1"
    state = connectors.load_state("example-app", "1.5.0-rc0")
    assert state["connectors"]["platform-build"]["result"]["polled"] == "job-1"
    main(["connectors", "status", "1.5.0-rc0"])
    assert "platform-build: ok" in capsys.readouterr().out


def test_connectors_run_from_blocked_deploy_exits_1_with_reason(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_fakes(("platform-build", "fail"), ("platform-deploy", "blocked"), platform_build={"break_on_error": False})
    with pytest.raises(SystemExit):
        main(["-rc", "--defaults"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main(["connectors", "run", "1.5.0-rc0", "--from", "platform-deploy", "--defaults"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert (
        "platform-deploy: failed: blocked: platform-build did not reach SUCCESS (FAILURE: tests failed); "
        "deploy needs a SUCCESS build"
    ) in captured.out
    assert "does not apply" not in captured.out
    assert "a connector did not finish for 1.5.0-rc0" in captured.err


def test_connectors_run_refuses_unpushed_tag(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_fakes(("platform-build", "echo"))
    with pytest.raises(SystemExit):
        main(["connectors", "run", "9.9.9-rc0"])
    assert "not on origin" in capsys.readouterr().err


def test_state_file_is_plain_json(repo: Path) -> None:
    install_fakes(("platform-build", "echo"))
    main(["-rc", "--defaults"])
    path = connectors.state_path("example-app", "1.5.0-rc0")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tags"] == list(TAGS) and data["connectors"]["platform-build"]["status"] == "ok"


def test_log_lines_stay_in_order_with_connector_stderr_when_piped(repo: Path) -> None:
    install_fakes(("platform-build", "questions-exit"))
    out = subprocess.run(
        [sys.executable, "-c", "from release_cli.cli import main; main(['-rc', '--defaults'])"],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    ).stdout
    assert out.index("RELEASE example-app-1.5.0-rc0 FINISHED!") < out.index("CONNECTOR platform-build")
    assert out.index("CONNECTOR platform-build") < out.index("[platform-build] questions exploded")


def test_rejected_commit_rolls_back_cleanly(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint failed' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(SystemExit) as exc:
        main(["-rc", "--defaults"])
    assert exc.value.code == 1
    assert "lint failed" in capsys.readouterr().err
    assert local_tags(repo) == set() and remote_tags(repo) == set()
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") == ""


def test_ctrl_c_in_after_hook_restores_version_file(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from release_cli import hooks

    def interrupted(_cmd: str) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(hooks, "run_command", interrupted)
    write_config(repo, hooks='[[hooks]]\nwhen = "after"\ncmd = "mvn deploy"\n')
    with pytest.raises(SystemExit) as exc:
        main(["-rc", "--defaults"])
    assert exc.value.code == 130
    assert git(repo, "status", "--porcelain") == ""
    assert local_tags(repo) == set() and remote_tags(repo) == set()


def test_ctrl_c_after_the_push_keeps_the_release(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    real_push = gitops.push_atomic

    def push_then_interrupt(tags: list[str]) -> None:
        real_push(tags)
        raise KeyboardInterrupt

    monkeypatch.setattr(gitops, "push_atomic", push_then_interrupt)
    with pytest.raises(SystemExit) as exc:
        main(["-rc", "--defaults"])
    assert exc.value.code == 130
    assert remote_tags(repo) == set(TAGS) and local_tags(repo) == set(TAGS)
    assert git(repo, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")
    assert "release connectors run 1.5.0-rc0" in capsys.readouterr().err


def test_branch_behind_origin_stops_before_any_hook(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    other = tmp_path / "other"
    git(tmp_path, "clone", str(tmp_path / "example-app.git"), str(other))
    (other / "NOTES.md").write_text("from a teammate\n", encoding="utf-8")
    git(other, "add", ".")
    git(other, "commit", "-m", "teammate change")
    git(other, "push", "origin", "main")
    marker = tmp_path / "deployed"
    write_config(repo, hooks=f'[[hooks]]\nwhen = "after"\ncmd = "touch {marker}"\n')
    with pytest.raises(SystemExit) as exc:
        main(["-rc", "--defaults"])
    assert exc.value.code == 1
    assert "1 commit(s) behind origin/main" in capsys.readouterr().err
    assert not marker.exists()
    assert local_tags(repo) == set() and remote_tags(repo) == set()


def test_status_reports_open_rc_last_tag_and_origin(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from release_cli import commands

    install_fakes(("platform-build", "ok"))
    main(["-rc", "--defaults"])
    capsys.readouterr()
    data = commands.status_data(repo, fetch=True)
    assert data["version"] == "1.5.0-rc0-SNAPSHOT" and data["rc_open"]
    assert data["last_tag"] == "1.5.0-rc0"
    assert data["origin"] == {"behind": 0, "ahead": 0}
    assert data["connectors"] == {"platform-build": "ok"}


def test_undo_deletes_both_rc_tags_and_the_next_rc_moves_on(repo: Path) -> None:
    from release_cli import commands

    main(["-rc", "--defaults", "--skip-connectors"])
    commands.undo(repo, "1.5.0-rc0", force=False, ask=lambda _p: "n")
    assert remote_tags(repo) == set(TAGS)
    commands.undo(repo, "example-app-1.5.0-rc0", force=False, ask=lambda _p: "y")
    assert remote_tags(repo) == set() and local_tags(repo) == set()
    main(["-rc", "--defaults", "--skip-connectors"])
    assert remote_tags(repo) == {"1.5.0-rc1", "example-app-1.5.0-rc1"}


def test_undo_refuses_a_final_release(repo: Path) -> None:
    from release_cli import commands

    main(["-rc", "--defaults", "--skip-connectors"])
    main(["-fv", "--defaults", "--skip-connectors"])
    with pytest.raises(connectors.ConnectorError, match="final release"):
        commands.undo(repo, "1.5.0", force=True)
    assert "1.5.0" in remote_tags(repo)


def test_notes_lists_commits_since_previous_tag_without_release_commits(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from release_cli import commands

    main(["-rc", "--defaults", "--skip-connectors"])
    (repo / "feature.txt").write_text("x\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Add the feature")
    main(["-rc", "--defaults", "--skip-connectors"])
    capsys.readouterr()
    commands.notes(repo, None)
    out = capsys.readouterr().out
    assert "Changes since 1.5.0-rc0" in out and "Add the feature" in out
    assert "manual-release" not in out


def _cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", f"from release_cli.cli import main; main({list(args)!r})"],
        cwd=repo, capture_output=True, text=True, check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )


def test_json_stdout_is_one_object_even_with_hook_output(repo: Path) -> None:
    write_config(repo, hooks='[[hooks]]\nwhen = "before"\ncmd = "echo hook-noise"\n')
    git(repo, "add", "-f", ".release")
    git(repo, "commit", "-m", "config")
    done = _cli(repo, "-rc", "--defaults", "--json", "--skip-connectors")
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    assert data["ok"] and data["version"] == "1.5.0-rc0" and data["tags"] == list(TAGS)
    assert "hook-noise" in done.stderr and "STARTING RELEASE" in done.stderr


def test_dry_run_builds_into_a_temp_dir_and_tags_nothing(repo: Path) -> None:
    install_fakes(("build", "safe"), ("publish", "echo"))
    head = git(repo, "rev-parse", "HEAD")
    done = _cli(repo, "-rc", "--defaults", "--dry-run", "--json")
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    out_dir = Path(data["dry_run_dir"])
    assert out_dir == connectors.dry_run_dir({"repo": "example-app", "release_version": "1.5.0-rc0"})
    assert (out_dir / "build.txt").is_file()
    assert list(data["connectors"]) == ["build"] and data["connectors"]["build"]["result"]["sha"] == head
    assert data["connectors"]["build"]["result"]["tags"] == list(TAGS)
    assert remote_tags(repo) == set() and local_tags(repo) == set()
    assert connectors.load_state("example-app", "1.5.0-rc0") is None
    assert git(repo, "status", "--porcelain") == "" and git(repo, "rev-parse", "HEAD") == head


def test_dry_run_exits_1_when_a_safe_connector_fails(repo: Path) -> None:
    install_fakes(("build", "safe-fail"))
    done = _cli(repo, "-rc", "--defaults", "--dry-run", "--json")
    assert done.returncode == 1
    assert json.loads(done.stdout)["ok"] is False
    assert remote_tags(repo) == set() and local_tags(repo) == set()


def test_json_failure_is_still_json(repo: Path) -> None:
    (repo / ".gitignore").write_text(".release\nbuild/\n", encoding="utf-8")
    done = _cli(repo, "-rc", "--defaults", "--json")
    assert done.returncode == 1
    data = json.loads(done.stdout)
    assert data["ok"] is False and "working tree is not clean" in data["error"]


def test_quiet_prints_only_the_result_line(repo: Path) -> None:
    done = _cli(repo, "-rc", "--defaults", "--quiet", "--skip-connectors")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "RELEASE example-app-1.5.0-rc0 FINISHED!"


def test_quiet_still_shows_a_failed_connector_and_how_to_resume(repo: Path) -> None:
    install_fakes(("platform-build", "fail"))
    done = _cli(repo, "-rc", "--defaults", "--quiet")
    assert done.returncode == 1
    assert "platform-build: failed: FAILURE" in done.stderr
    assert "resume: release connectors run 1.5.0-rc0 --from platform-build" in done.stderr
    assert done.stdout.strip() == "RELEASE example-app-1.5.0-rc0 FINISHED!"


def test_release_from_a_branch_origin_does_not_have_yet(repo: Path) -> None:
    git(repo, "checkout", "-b", "release/next")
    main(["-rc", "--defaults", "--skip-connectors"])
    assert remote_tags(repo) == set(TAGS)
    assert git(repo, "rev-parse", "HEAD") == git(repo, "ls-remote", "origin", "refs/heads/release/next").split()[0]


def test_multi_module_release_bumps_child_parent_versions(repo: Path) -> None:
    pom = repo / "pom.xml"
    pom.write_text(
        pom.read_text(encoding="utf-8").replace(
            "<version>1.4.0-SNAPSHOT</version>",
            "<version>1.4.0-SNAPSHOT</version>\n\t<packaging>pom</packaging>\n\t<modules>\n\t\t<module>core</module>\n\t</modules>",
            1,
        ),
        encoding="utf-8",
    )
    (repo / "core").mkdir()
    (repo / "core" / "pom.xml").write_text(
        "<project>\n  <parent>\n    <groupId>com.example</groupId>\n    <artifactId>example-app</artifactId>\n"
        "    <version>1.4.0-SNAPSHOT</version>\n  </parent>\n  <artifactId>core</artifactId>\n</project>\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add core module")
    git(repo, "push", "origin", "main")
    main(["-rc", "--defaults", "--skip-connectors"])
    assert git(repo, "status", "--porcelain") == ""
    assert "<version>1.5.0-rc0</version>" in git(repo, "show", "1.5.0-rc0:core/pom.xml")
    assert "<version>1.5.0-rc0-SNAPSHOT</version>" in (repo / "core" / "pom.xml").read_text(encoding="utf-8")
