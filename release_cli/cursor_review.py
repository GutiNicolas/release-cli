"""Built-in connector `cursor-review`: a local Cursor `agent` looks at the deploy N minutes after SUCCESS.

The release does not wait. `run` leaves a detached job (one per repo + tag) that sleeps
until a wall-clock target, calls `agent` in ask mode, and posts a macOS notification.

    python -m release_cli.cursor_review questions|run   (protocol 1 on stdin/stdout)
    python -m release_cli.cursor_review worker <job.json>
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from release_cli.connectors import data_dir

DEFAULTS: dict[str, Any] = {
    "delay_minutes": 10,
    "notify_macos": True,
    "slack_cloud": False,
    "cloud_command": [],
    "agent_bin": "agent",
    "deploy_connector": "platform-deploy",
}
STALE_UNCLAIMED = 60.0
TICK = 60.0

BASE_PROMPT = """\
A release was just deployed. Look for errors caused by it and report what you find. Do not change code.

- repo: {repo}
- version: {version}
- environment: {environment}
- deploy type: {deploy_type}
- deploy job_id: {job_id} (status {status}, failure_reason: {failure_reason})
- deploy SUCCESS at: {succeeded_at} (UTC); this review runs {delay} minutes later
"""


def settings(config: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **config}


def _truthy(value: Any) -> bool:
    return value is True or str(value).lower() in ("true", "yes", "y", "1")


def questions(req: dict[str, Any]) -> dict[str, Any]:
    delay = settings(req.get("config", {}))["delay_minutes"]
    return {
        "protocol": 1,
        "questions": [
            {"id": "review", "type": "bool", "prompt": f"Schedule a Cursor review {delay} minutes after the deploy?", "default": True},
            {
                "id": "extra_prompt",
                "type": "text",
                "prompt": "Extra review prompt for this project (skills or MCPs to use, e.g. Datadog); empty for none",
                "required": False,
                "persist": "project",
                "when": {"review": True},
            },
        ],
    }


def _parse_time(value: Any, fallback: float) -> float:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_prompt(req: dict[str, Any], deploy: dict[str, Any], succeeded_epoch: float, delay: float, extra: str) -> str:
    text = BASE_PROMPT.format(
        repo=req["repo"],
        version=req["release_version"],
        environment=deploy.get("environment", "unknown"),
        deploy_type=deploy.get("deploy_type", "unknown"),
        job_id=deploy.get("job_id", "unknown"),
        status=deploy.get("status"),
        failure_reason=deploy.get("failure_reason"),
        succeeded_at=_iso(succeeded_epoch),
        delay=f"{delay:g}",
    )
    if extra.strip():
        text += f"\nProject instructions:\n{extra.strip()}\n"
    return text


# ---------- jobs ----------


def jobs_dir() -> Path:
    return data_dir() / "jobs"


def job_path(repo: str, tag: str) -> Path:
    return jobs_dir() / repo / f"{tag}.json"


def read_job(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_job(path: Path, job: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _active(job: dict[str, Any]) -> bool:
    return job.get("status") in ("pending", "running")


def cleanup_dead(now: Callable[[], float] = time.time) -> list[Path]:
    """Drop pending/running jobs whose worker died (reboot, kill). Finished jobs stay for `status`."""
    removed: list[Path] = []
    if not jobs_dir().is_dir():
        return removed
    for path in jobs_dir().glob("*/*.json"):
        job = read_job(path)
        if job is None or not _active(job):
            continue
        unclaimed = job.get("pid") is None and now() - path.stat().st_mtime < STALE_UNCLAIMED
        if unclaimed or pid_alive(job.get("pid")):
            continue
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def list_jobs(repo: str, tag: str | None = None) -> list[dict[str, Any]]:
    cleanup_dead()
    folder = jobs_dir() / repo
    paths = [folder / f"{tag}.json"] if tag else sorted(folder.glob("*.json"))
    jobs = [read_job(p) for p in paths if p.is_file()]
    return [j for j in jobs if j]


def _spawn(path: Path) -> int:
    log = path.with_suffix(".log").open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "release_cli.cursor_review", "worker", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    return proc.pid


def schedule(job: dict[str, Any], *, spawn: Callable[[Path], int] = _spawn) -> dict[str, Any]:
    """One job per repo + tag: a new one replaces (and stops) the pending one."""
    cleanup_dead()
    path = job_path(job["repo"], job["tag"])
    old = read_job(path)
    if old and _active(old) and pid_alive(old.get("pid")):
        try:
            os.kill(old["pid"], signal.SIGTERM)
        except OSError:
            pass
    job = {**job, "pid": None, "status": "pending", "output": str(path.with_suffix(".out"))}
    write_job(path, job)
    job["spawned_pid"] = spawn(path)
    return job


def _notify(title: str, message: str, run: Callable[..., Any]) -> None:
    if shutil.which("osascript") is None:
        return
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    run(["osascript", "-e", script], check=False, capture_output=True)


def agent_command(job: dict[str, Any]) -> list[str]:
    if job.get("cloud"):
        return [*job["cloud_command"], job["prompt"]]
    return [job["agent_bin"], "-p", "--mode", "ask", "--trust", "--workspace", job["cwd"], "--output-format", "text", job["prompt"]]


def work(
    path: Path,
    *,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    run: Callable[..., Any] = subprocess.run,
) -> str:
    """Wait for the wall-clock target, then review. Returns the final job status."""
    job = read_job(path)
    if job is None:
        return "missing"
    me = os.getpid()
    job["pid"] = me
    write_job(path, job)
    # Short ticks against the wall clock: after a laptop sleep past the target, it runs on wake.
    while (remaining := job["target_epoch"] - now()) > 0:
        sleep(min(remaining, TICK))
    current = read_job(path)
    if current is None or current.get("pid") != me:
        return "replaced"
    job = {**current, "status": "running", "started_at": _iso(now())}
    write_job(path, job)
    try:
        proc = run(agent_command(job), cwd=job["cwd"], capture_output=True, text=True, check=False)
        output, code = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except OSError as exc:
        output, code = f"could not start {agent_command(job)[0]}: {exc}\n", 127
    Path(job["output"]).write_text(output, encoding="utf-8")
    job = {**job, "status": "done" if code == 0 else "failed", "exit_code": code, "finished_at": _iso(now())}
    write_job(path, job)
    if job.get("notify"):
        verdict = "finished" if code == 0 else f"failed ({code})"
        _notify("release cursor-review", f"{job['repo']} {job['tag']}: review {verdict}. {job['output']}", run)
    return job["status"]


# ---------- protocol ----------


def run(req: dict[str, Any], *, now: Callable[[], float] = time.time, spawn: Callable[[Path], int] = _spawn) -> dict[str, Any]:
    answers = req.get("answers", {})
    cfg = settings(req.get("config", {}))
    if not answers.get("review"):
        return {"protocol": 1, "ok": True, "result": {"scheduled": False, "reason": "declined"}}
    name = cfg["deploy_connector"]
    deploy = (req.get("prior") or {}).get(name) or {}
    result = deploy.get("result") or {}
    if deploy.get("status") != "ok" or result.get("status") != "SUCCESS":
        return {"protocol": 1, "ok": True, "result": {"scheduled": False, "reason": f"no successful {name} in this chain"}}
    cloud = _truthy(cfg["slack_cloud"])
    if cloud and not cfg["cloud_command"]:
        return {
            "protocol": 1,
            "ok": False,
            "error": "slack_cloud is on but cloud_command is empty; set it with `release edit` or turn slack_cloud off",
            "result": {"scheduled": False},
        }
    delay = float(cfg["delay_minutes"])
    succeeded = _parse_time(result.get("succeeded_at"), now())
    target = succeeded + delay * 60
    job = schedule(
        {
            "repo": req["repo"],
            "tag": req["release_version"],
            "target_epoch": target,
            "target_at": _iso(target),
            "prompt": build_prompt(req, result, succeeded, delay, answers.get("extra_prompt") or ""),
            "cwd": req["repo_root"],
            "notify": _truthy(cfg["notify_macos"]),
            "cloud": cloud,
            "cloud_command": list(cfg["cloud_command"]),
            "agent_bin": cfg["agent_bin"],
        },
        spawn=spawn,
    )
    print(f"review scheduled for {job['target_at']} (pid {job['spawned_pid']}); see `release connectors status {req['release_version']}`", file=sys.stderr)
    return {
        "protocol": 1,
        "ok": True,
        "result": {"scheduled": True, "target_at": job["target_at"], "pid": job["spawned_pid"], "mode": "cloud" if cloud else "local"},
    }


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "worker":
        work(Path(sys.argv[2]))
        return
    req = json.load(sys.stdin)
    action = req.get("action", action)
    if action == "questions":
        resp = questions(req)
    elif action == "run":
        resp = run(req)
    else:
        print(f"unknown action {action!r}", file=sys.stderr)
        raise SystemExit(2)
    json.dump(resp, sys.stdout)


if __name__ == "__main__":
    main()
