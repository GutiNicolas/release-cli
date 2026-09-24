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

TAGS = ("1.5.0-rc0", "fraud-juggler-1.5.0-rc0")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote = tmp_path / "fraud-juggler.git"
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
    state = connectors.load_state("fraud-juggler", "1.5.0-rc0")
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
    state = connectors.load_state("fraud-juggler", "1.5.0-rc0")
    assert state["connectors"]["platform-deploy"]["status"] == "ok"


def test_only_runs_one_with_other_saved_results(repo: Path) -> None:
    install_fakes(("platform-build", "echo"), ("platform-deploy", "echo"), ("cursor-review", "echo"))
    main(["-rc", "--defaults"])
    main(["connectors", "run", "fraud-juggler-1.5.0-rc0", "--only", "platform-build", "--defaults"])
    calls = run_requests(repo.parent)
    assert [c["connector"] for c in calls][3:] == ["platform-build"]
    assert sorted(calls[-1]["prior"]) == ["cursor-review", "platform-deploy"]


def test_interrupted_job_is_repolled_not_relaunched(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_fakes(("platform-build", "interrupt"))
    with pytest.raises(SystemExit):
        main(["-rc", "--defaults"])
    out = capsys.readouterr().out
    assert "job_id: job-1" in out
    state = connectors.load_state("fraud-juggler", "1.5.0-rc0")
    assert state["connectors"]["platform-build"] == {**state["connectors"]["platform-build"], "status": "interrupted", "job_id": "job-1"}
    main(["connectors", "run", "1.5.0-rc0", "--defaults"])
    calls = run_requests(repo.parent)
    assert calls[0]["resume_job_id"] is None
    assert calls[1]["resume_job_id"] == "job-1"
    state = connectors.load_state("fraud-juggler", "1.5.0-rc0")
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
    path = connectors.state_path("fraud-juggler", "1.5.0-rc0")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tags"] == list(TAGS) and data["connectors"]["platform-build"]["status"] == "ok"


def test_log_lines_stay_in_order_with_connector_stderr_when_piped(repo: Path) -> None:
    install_fakes(("platform-build", "questions-exit"))
    out = subprocess.run(
        [sys.executable, "-c", "from release_cli.cli import main; main(['-rc', '--defaults'])"],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    ).stdout
    assert out.index("RELEASE fraud-juggler-1.5.0-rc0 FINISHED!") < out.index("CONNECTOR platform-build")
    assert out.index("CONNECTOR platform-build") < out.index("[platform-build] questions exploded")
