"""Protocol 1 against real subprocesses: tests/fixtures/fake_connector.py run with sys.executable."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from release_cli import commands, connectors
from release_cli.cli import main
from release_cli.config import load
from tests.conftest import write_config

FAKE = str(Path(__file__).parent / "fixtures" / "fake_connector.py")
POM = Path(__file__).parent / "fixtures" / "juggler-like.pom.xml"


def install_fakes(*specs: tuple[str, str], **flags: dict[str, Any]) -> None:
    """specs: (name, behavior). flags: name -> extra entry keys (break_on_error, enabled, ...)."""
    connectors.save_global(
        {
            "order": [name for name, _ in specs],
            "connectors": {
                name: {"command": [sys.executable, FAKE, behavior], "enabled": True, "break_on_error": True, **flags.get(name.replace("-", "_"), {})}
                for name, behavior in specs
            },
        }
    )


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pom.xml").write_text(POM.read_text(encoding="utf-8"), encoding="utf-8")
    write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.jsonl"))
    return tmp_path


def calls(project: Path) -> list[dict[str, Any]]:
    path = project / "calls.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def run(project: Path, answers: list[str] | None = None, *, defaults: bool = False, dry_run: bool = False) -> tuple[bool, dict[str, Any]]:
    cfg = load(project)
    assert cfg is not None
    replies = iter(answers or [])
    ok = commands.post_push(project, cfg, "1.5.0-rc0", "abc123", defaults=defaults, dry_run=dry_run, ask=lambda _p: next(replies))
    state = connectors.load_state(project.name, "1.5.0-rc0") or {}
    return ok, state.get("connectors", {})


def run_requests(project: Path, name: str | None = None) -> list[dict[str, Any]]:
    return [c["request"] for c in calls(project) if c["action"] == "run" and (name is None or c["request"]["connector"] == name)]


def test_bool_choice_text_answers_and_text_persisted(project: Path) -> None:
    install_fakes(("a", "types"))
    ok, state = run(project, ["y", "dev", "FRAUD"])
    assert ok and state["a"]["status"] == "ok"
    assert state["a"]["result"]["answers"] == {"go": True, "env": "dev", "key": "FRAUD"}
    assert load(project).connectors["a"] == {"key": "FRAUD"}
    ok, state = run(project, ["", ""])
    assert state["a"]["result"]["answers"] == {"go": False, "env": "qa", "key": "FRAUD"}
    assert run_requests(project)[-1]["config"] == {"key": "FRAUD"}


def test_defaults_takes_each_default_and_never_invents_text(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_fakes(("a", "types"))
    ok, state = run(project, defaults=True)
    assert not ok
    assert state["a"]["status"] == "failed"
    assert "release edit config set a key <value>" in state["a"]["error"]
    assert run_requests(project) == []
    commands.edit(project, ["config", "set", "a", "key", "FRAUD"])
    ok, state = run(project, defaults=True)
    assert ok and state["a"]["result"]["answers"] == {"go": False, "env": "qa", "key": "FRAUD"}


@pytest.mark.parametrize(
    ("behavior", "reason"),
    [
        ("broken-json", "not valid JSON"),
        ("bad-protocol", "protocol must be 1, got 2"),
        ("missing-fields", "result must be an object"),
        ("exit-nonzero", "run exited 3; stderr tail: kaput"),
        ("questions-exit", "questions exited 4"),
    ],
)
def test_invalid_responses_fail_with_reason(project: Path, behavior: str, reason: str, capsys: pytest.CaptureFixture[str]) -> None:
    install_fakes(("a", behavior))
    ok, state = run(project)
    assert not ok
    assert state["a"]["status"] == "failed"
    assert reason in state["a"]["error"]
    if behavior in ("exit-nonzero", "questions-exit"):
        assert "[a] " in capsys.readouterr().err


def test_questions_timeout(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connectors, "QUESTIONS_TIMEOUT", 0.5)
    install_fakes(("a", "slow-questions"))
    ok, state = run(project)
    assert not ok and "questions timed out" in state["a"]["error"]


def test_run_timeout_is_interrupted_with_job_id(project: Path) -> None:
    install_fakes(("a", "slow-run"))
    ok, state = run(project)
    assert not ok
    assert state["a"]["status"] == "interrupted"
    assert state["a"]["job_id"] == "job-slow"


def test_global_order_with_project_override(project: Path) -> None:
    install_fakes(("a", "echo"), ("b", "echo"), ("c", "echo"))
    run(project)
    assert [r["connector"] for r in run_requests(project)] == ["a", "b", "c"]
    commands.edit(project, ["connector", "move", "c", "1", "--project"])
    commands.edit(project, ["connector", "set", "b", "enabled", "n", "--project"])
    (project / "calls.jsonl").unlink()
    run(project)
    assert [r["connector"] for r in run_requests(project)] == ["c", "a"]
    assert load(project).connectors["order"] == ["c", "a", "b"]
    g = connectors.load_global()
    assert g["order"] == ["a", "b", "c"] and g["connectors"]["b"]["enabled"] is True


def test_break_on_error_true_stops_chain(project: Path) -> None:
    install_fakes(("build", "fail"), ("deploy", "echo"))
    ok, state = run(project)
    assert not ok
    assert "deploy" not in state
    assert [r["connector"] for r in run_requests(project)] == ["build"]


def test_break_on_error_false_continues_and_prior_carries_failure(project: Path) -> None:
    install_fakes(("build", "fail"), ("deploy", "echo"), build={"break_on_error": False})
    ok, state = run(project)
    assert not ok
    assert state["deploy"]["status"] == "ok"
    prior = run_requests(project, "deploy")[0]["prior"]
    assert prior["build"]["status"] == "failed"
    assert prior["build"]["result"] == {"status": "FAILURE"}


def test_prior_passes_result_from_one_to_the_next(project: Path) -> None:
    install_fakes(("a", "echo"), ("b", "echo"))
    run(project)
    b = run_requests(project, "b")[0]
    assert b["prior"]["a"]["status"] == "ok"
    assert b["prior"]["a"]["result"]["by"] == "a"
    for key in ("repo", "artifact", "release_version", "tags", "sha", "mode", "dry_run", "answers"):
        assert key in b
    assert b["tags"] == ["1.5.0-rc0", "fraud-juggler-1.5.0-rc0"] and b["mode"] == "rc"


def test_not_applies_skips_run(project: Path) -> None:
    install_fakes(("a", "not-applies"), ("b", "echo"))
    ok, state = run(project)
    assert ok and "a" not in state
    assert [r["connector"] for r in run_requests(project)] == ["b"]


def test_dry_run_shows_questions_and_never_calls_run(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_fakes(("a", "types"), ("b", "echo"))
    ok, state = run(project, dry_run=True)
    assert ok and state == {}
    assert all(c["action"] == "questions" and c["request"]["dry_run"] for c in calls(project))
    out = capsys.readouterr().out
    assert "? Project key" in out and "run not called" in out
    assert "a" not in load(project).connectors


def test_release_dry_run_lists_connector_questions(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_fakes(("a", "ok"))
    main(["-rc", "--dry-run"])
    assert "? Go? (y/n) [y]" in capsys.readouterr().out
    assert run_requests(project) == []
    main(["-rc", "--dry-run", "--skip-connectors"])
    assert "CONNECTOR a" not in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["-y", "--yes"])
def test_yes_is_deprecated_alias_of_defaults(project: Path, flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    main(["-rc", "--dry-run", flag])
    assert "deprecated; use --defaults" in capsys.readouterr().err


def test_yes_on_init_warns_and_takes_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pom.xml").write_text(POM.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    main(["--init", "-y"])
    assert "deprecated" in capsys.readouterr().err
    assert (tmp_path / ".release").is_file()
