"""cursor-review: fake `agent` / `osascript` on PATH, injected clock and sleep."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from release_cli import cursor_review as cr

SUCCESS_AT = "2026-09-23T19:40:00Z"
SUCCESS_EPOCH = 1790192400.0


@pytest.fixture()
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("agent", "osascript"):
        script = bin_dir / name
        script.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            f"open({str(tmp_path / (name + '.calls'))!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print('no errors found')\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path


def recorded(root: Path, name: str) -> list[list[str]]:
    path = root / f"{name}.calls"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def request(tmp_path: Path, *, extra: str = "Use the Datadog MCP.", deploy_status: str = "SUCCESS", notify: bool = True) -> dict:
    return {
        "protocol": 1,
        "action": "run",
        "connector": "cursor-review",
        "repo": "fraud-juggler",
        "repo_root": str(tmp_path),
        "release_version": "1.5.0",
        "answers": {"review": True, "extra_prompt": extra},
        "config": {"notify_macos": notify, "slack_cloud": False},
        "prior": {
            "platform-deploy": {
                "status": "ok" if deploy_status == "SUCCESS" else "failed",
                "result": {"job_id": "dep-7", "status": deploy_status, "environment": "prd", "deploy_type": "SAFE_DEPLOY", "succeeded_at": SUCCESS_AT},
            }
        },
    }


def scheduled(tmp_path: Path, **kw) -> Path:
    resp = cr.run(request(tmp_path, **kw), spawn=lambda _p: 4242)
    assert resp["ok"] and resp["result"]["scheduled"] is True
    return cr.job_path("fraud-juggler", "1.5.0")


class Clock:
    def __init__(self, start: float):
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_timer_is_ten_minutes_after_deploy_success(fake_bin: Path) -> None:
    path = scheduled(fake_bin)
    job = cr.read_job(path)
    assert job["target_epoch"] == SUCCESS_EPOCH + 600
    clock = Clock(SUCCESS_EPOCH + 30)
    assert cr.work(path, now=clock.now, sleep=clock.sleep) == "done"
    assert sum(clock.sleeps) == pytest.approx(570)
    assert max(clock.sleeps) <= cr.TICK
    [argv] = recorded(fake_bin, "agent")
    assert argv[:3] == ["-p", "--mode", "ask"]
    prompt = argv[-1]
    for needle in ("fraud-juggler", "1.5.0", "prd", "SAFE_DEPLOY", "dep-7", "2026-09-23T19:40:00Z", "Use the Datadog MCP."):
        assert needle in prompt
    assert Path(job["output"]).read_text(encoding="utf-8").strip() == "no errors found"
    assert len(recorded(fake_bin, "osascript")) == 1


def test_target_already_passed_on_wake_runs_immediately(fake_bin: Path) -> None:
    path = scheduled(fake_bin)
    clock = Clock(SUCCESS_EPOCH + 3 * 3600)
    assert cr.work(path, now=clock.now, sleep=clock.sleep) == "done"
    assert clock.sleeps == []
    assert len(recorded(fake_bin, "agent")) == 1


def test_notification_off_never_calls_osascript(fake_bin: Path) -> None:
    path = scheduled(fake_bin, notify=False)
    clock = Clock(SUCCESS_EPOCH + 601)
    cr.work(path, now=clock.now, sleep=clock.sleep)
    assert len(recorded(fake_bin, "agent")) == 1
    assert recorded(fake_bin, "osascript") == []


def test_no_successful_deploy_schedules_nothing(fake_bin: Path) -> None:
    resp = cr.run(request(fake_bin, deploy_status="FAILURE"), spawn=lambda _p: pytest.fail("spawned"))
    assert resp["ok"] and resp["result"]["scheduled"] is False


def test_slack_cloud_without_command_fails_clearly(fake_bin: Path) -> None:
    req = request(fake_bin)
    req["config"]["slack_cloud"] = True
    resp = cr.run(req, spawn=lambda _p: pytest.fail("spawned"))
    assert resp["ok"] is False and "cloud_command" in resp["error"]


def test_same_repo_and_tag_replaces_pending_job(fake_bin: Path) -> None:
    path = scheduled(fake_bin)
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    job = cr.read_job(path)
    cr.write_job(path, {**job, "pid": old.pid})
    scheduled(fake_bin, extra="second")
    assert old.wait(timeout=5) != 0
    assert "second" in cr.read_job(path)["prompt"]
    assert len(list(path.parent.glob("*.json"))) == 1


def test_replaced_worker_exits_without_calling_agent(fake_bin: Path) -> None:
    path = scheduled(fake_bin)
    clock = Clock(SUCCESS_EPOCH)

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        cr.write_job(path, {**cr.read_job(path), "pid": 1})

    assert cr.work(path, now=clock.now, sleep=sleep) == "replaced"
    assert recorded(fake_bin, "agent") == []


def test_dead_pid_is_cleaned_but_finished_jobs_stay(fake_bin: Path) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    pending = cr.job_path("fraud-juggler", "1.4.0")
    done = cr.job_path("fraud-juggler", "1.3.0")
    cr.write_job(pending, {"status": "pending", "pid": dead.pid, "target_at": "x", "output": "x"})
    cr.write_job(done, {"status": "done", "pid": dead.pid, "target_at": "x", "output": "x"})
    assert cr.cleanup_dead() == [pending]
    assert not pending.exists() and done.exists()
    assert [j["status"] for j in cr.list_jobs("fraud-juggler")] == ["done"]


def test_protocol_questions_over_subprocess(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "release_cli.cursor_review", "questions"],
        input=json.dumps({"protocol": 1, "action": "questions", "config": {}}),
        capture_output=True,
        text=True,
        check=True,
    )
    resp = json.loads(proc.stdout)
    assert resp["protocol"] == 1
    assert [q["id"] for q in resp["questions"]] == ["review", "extra_prompt"]
    assert resp["questions"][1]["persist"] == "project"
