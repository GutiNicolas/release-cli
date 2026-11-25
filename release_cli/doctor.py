"""`release doctor`: say why `release` would not work here. Read-only: it never writes, fetches, or runs a connector."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from release_cli import adapters, gitops, update
from release_cli import connectors as cx
from release_cli.adapters.base import AdapterError
from release_cli.config import LOCAL_NAME, TEAM_NAME, ConfigError, load, loads_toml
from release_cli.ui import info
from release_cli.versioning import PlanError, parse

OK, WARN, FAIL = "ok", "warn", "fail"
SUBPROCESS_TIMEOUT = 20.0
# Same lookup as the connector shim, but it only loads the entry point; nothing runs.
LOAD_ENTRY_POINT = (
    "import sys\n"
    "from importlib.metadata import entry_points\n"
    "(ep,) = entry_points(group='release.connectors', name=sys.argv[1])\n"
    "ep.load()\n"
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str
    fix: str = ""


def _ok(name: str, message: str) -> Check:
    return Check(name, OK, message)


def _warn(name: str, message: str, fix: str = "") -> Check:
    return Check(name, WARN, message, fix)


def _fail(name: str, message: str, fix: str = "") -> Check:
    return Check(name, FAIL, message, fix)


def _probe(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str] | None:
    """Run cmd to completion within a short timeout; None when it cannot start or hangs."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=SUBPROCESS_TIMEOUT, cwd=cwd, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _last_line(proc: subprocess.CompletedProcess[str]) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1].strip() if lines else f"exit {proc.returncode}"


def _read_toml(path: Path, name: str, *, what: str) -> tuple[dict[str, Any] | None, Check]:
    if not path.is_file():
        return None, _ok(name, f"{path} not present (fine, {what})")
    try:
        return loads_toml(path.read_text(encoding="utf-8")), _ok(name, f"{path} parses")
    except (ConfigError, OSError) as exc:
        return None, _fail(name, f"{path}: {exc}", f"fix or delete {path}")


# ---------- install ----------


def check_install(global_cfg: dict[str, Any] | None) -> list[Check]:
    out: list[Check] = []
    git_ok = shutil.which("git") is not None
    out.append(_ok("tool git", "found") if git_ok else _fail("tool git", "git not found", "install git"))
    uv = shutil.which("uv") is not None
    out.append(
        _ok("tool uv", "found")
        if uv
        else _warn("tool uv", "uv not found; connectors installed from a URL need it", update.uv_install_hint())
    )
    if global_cfg and "github-release" in global_cfg["connectors"]:
        out.extend(_check_gh())
    channel = update.managed_channel()
    if channel is not None:
        version = update._installed_version() or "?"
        out.append(_ok("install", f"release-cli {version}, installed with {channel.name} (upgrade: {channel.upgrade})"))
        return out
    if not git_ok:
        return out
    src = update.find_source()
    if src is None:
        out.append(
            _warn(
                "install",
                "release-cli source clone not found; `release update` will not work",
                f"git clone {update.INSTALL_URL} && cd release-cli && ./install.sh",
            )
        )
        return out
    installed = update._installed_version()
    clone = update._pyproject_version(src.path)
    if installed is None:
        out.append(_warn("install", f"release-cli is not installed as a package (clone {src.path} is {clone})", f"cd {src.path} && ./install.sh"))
    elif clone != "?" and installed != clone:
        out.append(_warn("install", f"installed {installed}, clone {src.path} is {clone}", "release update"))
    else:
        out.append(_ok("install", f"release-cli {installed}, clone {src.path} ({src.found_in})"))
    return out


def _check_gh() -> list[Check]:
    if shutil.which("gh") is None:
        return [_fail("tool gh", "gh not found; the github-release connector needs it", "brew install gh (https://cli.github.com)")]
    proc = _probe(["gh", "auth", "status"])
    if proc is None:
        return [_warn("tool gh", "`gh auth status` did not finish", "run `gh auth status` yourself")]
    if proc.returncode != 0:
        return [_fail("tool gh", f"gh is not logged in ({_last_line(proc)})", "gh auth login")]
    return [_ok("tool gh", "found and logged in")]


# ---------- global config and connectors ----------


def check_global() -> tuple[list[Check], dict[str, Any] | None]:
    out: list[Check] = []
    _, check = _read_toml(cx.global_path(), "config connectors.toml", what="no connectors installed")
    out.append(check)
    broken = check.status == FAIL
    _, check = _read_toml(cx.release_toml_path(), "config release.toml", what="defaults apply")
    out.append(check)
    _, check = _read_toml(update.source_toml_path(), "config source.toml", what="the uv receipt is used instead")
    out.append(check)
    if broken:
        return out, None
    try:
        return out, cx.load_global()
    except (ConfigError, OSError) as exc:
        out.append(_fail("config connectors.toml", str(exc), f"fix {cx.global_path()}"))
        return out, None


def check_connector(name: str, entry: Any) -> Check:
    label = f"connector {name}"
    if not isinstance(entry, dict):
        return _fail(label, "entry is not a table in connectors.toml", f"release connector remove {name} (or fix {cx.global_path()})")
    if isinstance(entry.get("command"), list):
        exe = str(entry["command"][0]) if entry["command"] else ""
        found = exe and (shutil.which(exe) or Path(exe).is_file())
        return _ok(label, f"custom command {exe}") if found else _fail(label, f"command {exe or '(empty)'} not found", f"fix command in {cx.global_path()}")
    if entry.get("builtin"):
        return _ok(label, "built-in") if name in cx.BUILTINS else _fail(label, "unknown built-in", f"release connector remove {name}")
    reinstall = f"release connector update {name}"
    python = cx.env_python()
    if not python.is_file():
        return _fail(label, f"connector environment missing ({python})", reinstall)
    entry_point = str(entry.get("entry_point") or name)
    proc = _probe([str(python), "-c", LOAD_ENTRY_POINT, entry_point])
    if proc is None:
        return _fail(label, f"loading entry point {entry_point} did not finish or the environment python cannot start", reinstall)
    if proc.returncode != 0:
        return _fail(label, f"entry point {entry_point} does not load ({_last_line(proc)})", reinstall)
    return _ok(label, f"entry point {entry_point} loads")


def check_connectors(global_cfg: dict[str, Any] | None) -> list[Check]:
    if global_cfg is None:
        return []
    return [check_connector(name, entry) for name, entry in global_cfg["connectors"].items()]


# ---------- project ----------


def has_project_files(cwd: Path) -> bool:
    return (cwd / LOCAL_NAME).is_file() or (cwd / TEAM_NAME).is_file()


def check_project(cwd: Path, global_cfg: dict[str, Any] | None) -> list[Check]:
    if not has_project_files(cwd):
        return [_ok("project", f"no {LOCAL_NAME} or {TEAM_NAME} here; run doctor inside a project to check it too")]
    out: list[Check] = []
    try:
        cfg = load(cwd)
    except ConfigError as exc:
        return [_fail("project config", str(exc), f"fix {LOCAL_NAME} / {TEAM_NAME}, or `release --init`")]
    assert cfg is not None
    out.append(_ok("project config", f"{cfg.tool}, artifact {cfg.artifact}"))
    try:
        version = adapters.get(cfg.tool).read(cwd, cfg).version
        parse(version)
        out.append(_ok("project version", f"{version} in {cfg.version_file}"))
    except (AdapterError, PlanError) as exc:
        out.append(_fail("project version", str(exc), f"check version_file ({cfg.version_file}) in the project config"))
    if cfg.tool == "go":
        out.append(_check_go())
    out.extend(_check_overrides(cfg.connectors, global_cfg))
    out.extend(_check_git(cwd))
    return out


def _check_go() -> Check:
    if shutil.which("go") is None:
        return _warn("tool go", "go not found; `go test` hooks and Go builds need it", "install Go (https://go.dev/dl)")
    return _ok("tool go", "found")


def _check_overrides(project: dict[str, Any], global_cfg: dict[str, Any] | None) -> list[Check]:
    if global_cfg is None or not project:
        return []
    known = global_cfg["connectors"]
    unknown = [
        name
        for key, value in project.items()
        for name in (value if key in cx.CONNECTOR_LISTS else [key])
        if name not in known
    ]
    if not unknown:
        return [_ok("project connectors", "overrides name installed connectors")]
    return [_warn("project connectors", f"project config names connectors that are not installed: {', '.join(sorted(set(unknown)))}", "release connector add <url>, or remove them from the project config")]


def _check_git(cwd: Path) -> list[Check]:
    if shutil.which("git") is None:
        return []
    probe = gitops.run(["git", "rev-parse", "--is-inside-work-tree"], check=False)
    if probe.returncode != 0:
        return [_fail("git repo", "not a git repository", "git init, or run release from the project root")]
    try:
        repo = gitops.current_repo()
    except gitops.GitError as exc:
        return [_fail("git repo", str(exc), "git checkout <branch>")]
    out = [_ok("git repo", f"on branch {repo.branch}")]
    url = gitops.remote_url()
    if not url:
        return out + [_fail("git origin", "no remote named origin", "git remote add origin <url>")]
    # Read-only on purpose: ls-remote instead of fetch, so doctor never moves origin/<branch>.
    ls = _probe(["git", "ls-remote", "--heads", "origin", f"refs/heads/{repo.branch}"], cwd=cwd)
    if ls is None or ls.returncode != 0:
        why = _last_line(ls) if ls else "timed out"
        return out + [_fail("git origin", f"origin unreachable ({why})", "check network, VPN, and git credentials for origin")]
    out.append(_ok("git origin", f"origin reachable ({url})"))
    remote_sha = ls.stdout.split()[0] if ls.stdout.strip() else ""
    if not remote_sha or remote_sha == repo.sha:
        out.append(_ok("git sync", "up to date with origin" if remote_sha else f"origin has no {repo.branch} yet (the push creates it)"))
        return out
    contains = gitops.run(["git", "merge-base", "--is-ancestor", remote_sha, "HEAD"], check=False)
    if contains.returncode == 0:
        out.append(_ok("git sync", "not behind origin"))
    else:
        out.append(_fail("git sync", f"{repo.branch} is behind or diverged from origin/{repo.branch}", f"git pull --rebase origin {repo.branch}"))
    return out


# ---------- run ----------


def diagnose(cwd: Path) -> list[Check]:
    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
    os.environ.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    global_checks, global_cfg = check_global()
    return [*check_install(global_cfg), *global_checks, *check_connectors(global_cfg), *check_project(cwd, global_cfg)]


MARK = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}


def doctor(cwd: Path, *, as_json: bool) -> int:
    checks = diagnose(cwd)
    if as_json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        for c in checks:
            info(f"{MARK[c.status]}  {c.name}: {c.message}")
            if c.fix and c.status != OK:
                info(f"      -> {c.fix}")
        failed = sum(c.status == FAIL for c in checks)
        warned = sum(c.status == WARN for c in checks)
        info(f"{failed} failed, {warned} warnings" if failed or warned else "all good")
    return 1 if any(c.status == FAIL for c in checks) else 0


def main_exit(cwd: Path, *, as_json: bool) -> None:
    code = doctor(cwd, as_json=as_json)
    if code:
        sys.exit(code)
