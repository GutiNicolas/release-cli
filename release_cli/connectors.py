"""Connectors: post-push steps run as subprocesses speaking JSON protocol 1.

A connector never touches the tag. It gets a context on stdin, answers on stdout,
and logs on stderr. See README "Connectors" for the contract.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from release_cli import prompts
from release_cli.config import CONNECTOR_LISTS, ConfigError, dump_toml, loads_toml, set_connector_value

PROTOCOL = 1
QUESTIONS_TIMEOUT = 30.0
DEFAULT_RUN_TIMEOUT = 3600.0
STOP_GRACE = 10.0
STDERR_TAIL = 20
BUILTINS = {"cursor-review": "release_cli.cursor_review"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
QUESTION_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
STATUSES = ("ok", "failed", "interrupted")
ENTRY_POINT_SHIM = (
    "import sys\n"
    "from importlib.metadata import entry_points\n"
    "name = sys.argv.pop(1)\n"
    "(ep,) = entry_points(group='release.connectors', name=name)\n"
    "ep.load()()\n"
)


class ConnectorError(RuntimeError):
    pass


class ChainInterrupted(RuntimeError):
    """Ctrl-C during the post-push phase. The state is saved; the tag stays."""

    def __init__(self, connector: str):
        super().__init__(connector)
        self.connector = connector


# ---------- paths and global config ----------


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "release"


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "release"


def global_path() -> Path:
    return config_dir() / "connectors.toml"


def env_dir() -> Path:
    return data_dir() / "env"


def env_python() -> Path:
    return env_dir() / "bin" / "python"


def load_global() -> dict[str, Any]:
    path = global_path()
    data = loads_toml(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data.setdefault("order", [])
    data.setdefault("connectors", {})
    if not isinstance(data["order"], list) or not isinstance(data["connectors"], dict):
        raise ConfigError(f"{path}: order must be a list and [connectors] a table")
    return data


def save_global(data: dict[str, Any]) -> None:
    path = global_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dump_toml(data), encoding="utf-8")
    tmp.replace(path)


def check_name(name: str) -> None:
    if not NAME_RE.match(name) or name in CONNECTOR_LISTS:
        raise ConnectorError(f"invalid connector name: {name!r}")


@dataclass
class Connector:
    name: str
    command: list[str]
    enabled: bool = True
    break_on_error: bool = True
    timeout_seconds: float | None = None
    config: dict[str, Any] = field(default_factory=dict)
    origin: dict[str, str] = field(default_factory=dict)


def command_for(name: str, entry: dict[str, Any]) -> list[str]:
    if isinstance(entry.get("command"), list):
        return [str(part) for part in entry["command"]]
    if entry.get("builtin"):
        return [sys.executable, "-m", BUILTINS[name]]
    return [str(env_python()), "-c", ENTRY_POINT_SHIM, str(entry.get("entry_point") or name)]


def resolve(global_cfg: dict[str, Any], project: dict[str, Any], *, warn: Callable[[str], None] = lambda _m: None) -> list[Connector]:
    """All installed connectors in effective order. Project [connectors] overrides order/enabled/disabled."""
    known: dict[str, dict[str, Any]] = global_cfg["connectors"]
    order = [n for n in global_cfg["order"] if n in known] + [n for n in known if n not in global_cfg["order"]]
    order_origin = "global"
    for key in CONNECTOR_LISTS:
        for name in project.get(key, []):
            if name not in known:
                warn(f"connectors.{key} in project names unknown connector {name!r}; ignored")
    if project.get("order"):
        pinned = [n for n in project["order"] if n in known]
        order = pinned + [n for n in order if n not in pinned]
        order_origin = "project"
    out: list[Connector] = []
    for name in order:
        entry = known[name]
        enabled, enabled_origin = bool(entry.get("enabled", True)), "global"
        if name in project.get("enabled", []):
            enabled, enabled_origin = True, "project"
        if name in project.get("disabled", []):
            enabled, enabled_origin = False, "project"
        timeout = entry.get("timeout_seconds")
        out.append(
            Connector(
                name=name,
                command=command_for(name, entry),
                enabled=enabled,
                break_on_error=bool(entry.get("break_on_error", True)),
                timeout_seconds=float(timeout) if timeout else None,
                config={**entry.get("config", {}), **project.get(name, {})},
                origin={"order": order_origin, "enabled": enabled_origin, "break_on_error": "global"},
            )
        )
    return out


# ---------- protocol ----------


@dataclass(frozen=True)
class Question:
    id: str
    type: str
    prompt: str
    default: Any = None
    options: tuple[str, ...] = ()
    required: bool = True
    persist: str = "none"
    when: dict[str, Any] = field(default_factory=dict)
    default_when: tuple[tuple[dict[str, Any], Any], ...] = ()
    pattern: str | None = None
    lo: int | None = None
    hi: int | None = None

    def value_error(self, value: Any) -> str | None:
        """Why `value` is not a valid answer to this question, or None."""
        if self.type == "bool":
            return None if isinstance(value, bool) else "expected true or false"
        if self.type == "choice":
            return None if value in self.options else f"expected one of {', '.join(self.options)}"
        if self.type == "int":
            if type(value) is not int:
                return "expected a whole number"
            return prompts.int_error(str(value), self.lo, self.hi)
        if not isinstance(value, str):
            return "expected text"
        if self.pattern is not None and value and not re.fullmatch(self.pattern, value):
            return f"must match {self.pattern}"
        return None

    def default_for(self, answers: dict[str, Any]) -> Any:
        for cond, value in self.default_when:
            if _matches(cond, answers):
                return value
        return self.default


@dataclass
class Questions:
    applies: bool
    timeout_seconds: float | None
    items: list[Question]
    # applies=false + blocked=true: a prerequisite in `prior` failed (recorded failed, exit 1).
    # applies=false alone: this project is not for the connector (skipped, exit 0).
    blocked: bool = False
    reason: str | None = None


@dataclass
class Outcome:
    status: str
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    job_id: str | None = None
    ctrl_c: bool = False

    def entry(self) -> dict[str, Any]:
        return {"status": self.status, "result": self.result, "error": self.error, "job_id": self.job_id}


class ProtocolError(ValueError):
    pass


def _is_number(value: Any) -> bool:
    return type(value) is int or type(value) is float


def _check_envelope(resp: Any) -> dict[str, Any]:
    if not isinstance(resp, dict):
        raise ProtocolError("response is not a JSON object")
    proto = resp.get("protocol")
    if type(proto) is not int or proto != PROTOCOL:
        raise ProtocolError(f"protocol must be {PROTOCOL}, got {proto!r}")
    return resp


def parse_questions(resp: Any) -> Questions:
    resp = _check_envelope(resp)
    applies = resp.get("applies", True)
    if not isinstance(applies, bool):
        raise ProtocolError("applies must be true or false")
    blocked = resp.get("blocked", False)
    if not isinstance(blocked, bool):
        raise ProtocolError("blocked must be true or false")
    if blocked and applies:
        raise ProtocolError("blocked needs applies false")
    reason = resp.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ProtocolError("reason must be a string")
    timeout = resp.get("timeout_seconds")
    if timeout is not None and (not _is_number(timeout) or timeout <= 0):
        raise ProtocolError("timeout_seconds must be a positive number")
    raw = resp.get("questions")
    if not isinstance(raw, list):
        raise ProtocolError("missing questions list")
    items: list[Question] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw):
        items.append(_parse_question(item, idx, seen))
    return Questions(
        applies=applies,
        timeout_seconds=float(timeout) if timeout else None,
        items=items,
        blocked=blocked,
        reason=reason.strip() if reason and reason.strip() else None,
    )


def _parse_question(item: Any, idx: int, seen: set[str]) -> Question:
    where = f"questions[{idx}]"
    if not isinstance(item, dict):
        raise ProtocolError(f"{where} is not an object")
    qid, qtype, prompt = item.get("id"), item.get("type"), item.get("prompt")
    if not isinstance(qid, str) or not QUESTION_ID_RE.match(qid):
        raise ProtocolError(f"{where}.id must match [A-Za-z0-9_]+")
    if qid in seen:
        raise ProtocolError(f"duplicate question id {qid!r}")
    seen.add(qid)
    if qtype not in ("bool", "choice", "text", "int"):
        raise ProtocolError(f"{qid}: type must be bool, choice, text, or int")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ProtocolError(f"{qid}: missing prompt")
    persist = item.get("persist", "project" if qtype == "text" else "none")
    if persist not in ("project", "none"):
        raise ProtocolError(f"{qid}: persist must be project or none")
    earlier = seen - {qid}
    when = _parse_condition(item.get("when", {}), earlier, f"{qid}: when")
    options: tuple[str, ...] = ()
    required, pattern, lo, hi = True, None, None, None
    if qtype == "choice":
        raw_opts = item.get("options")
        if not isinstance(raw_opts, list) or not raw_opts or not all(isinstance(o, str) and o for o in raw_opts):
            raise ProtocolError(f"{qid}: options must be a non-empty list of strings")
        if len(set(raw_opts)) != len(raw_opts):
            raise ProtocolError(f"{qid}: duplicate options")
        options = tuple(raw_opts)
    if qtype in ("text", "int"):
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise ProtocolError(f"{qid}: required must be true or false")
    if qtype == "text" and "pattern" in item:
        pattern = item["pattern"]
        if not isinstance(pattern, str):
            raise ProtocolError(f"{qid}: pattern must be a string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ProtocolError(f"{qid}: invalid pattern: {exc}") from exc
    if qtype == "int":
        lo, hi = item.get("min"), item.get("max")
        if any(v is not None and type(v) is not int for v in (lo, hi)):
            raise ProtocolError(f"{qid}: min and max must be whole numbers")
        if lo is not None and hi is not None and lo > hi:
            raise ProtocolError(f"{qid}: min is greater than max")
    q = Question(qid, qtype, prompt.strip(), item.get("default"), options, required, persist, when, (), pattern, lo, hi)
    if q.default is not None or qtype in ("bool", "choice"):
        if (err := q.value_error(q.default)) is not None:
            raise ProtocolError(f"{qid}: default: {err}")
    raw_dw = item.get("default_when", [])
    if not isinstance(raw_dw, list):
        raise ProtocolError(f"{qid}: default_when must be a list")
    rules: list[tuple[dict[str, Any], Any]] = []
    for pos, rule in enumerate(raw_dw):
        where_rule = f"{qid}: default_when[{pos}]"
        if not isinstance(rule, dict) or set(rule) != {"if", "default"}:
            raise ProtocolError(f"{where_rule} must be {{\"if\": {{...}}, \"default\": ...}}")
        cond = _parse_condition(rule["if"], earlier, f"{where_rule}.if")
        if (err := q.value_error(rule["default"])) is not None:
            raise ProtocolError(f"{where_rule}.default: {err}")
        rules.append((cond, rule["default"]))
    return replace(q, default_when=tuple(rules))


def _parse_condition(raw: Any, earlier: set[str], where: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or not all(isinstance(k, str) and k in earlier for k in raw):
        raise ProtocolError(f"{where} must be an object keyed by earlier question ids")
    return raw


def parse_run(resp: Any) -> Outcome:
    resp = _check_envelope(resp)
    ok = resp.get("ok")
    if not isinstance(ok, bool):
        raise ProtocolError("ok must be true or false")
    result = resp.get("result")
    if not isinstance(result, dict):
        raise ProtocolError("result must be an object")
    status = resp.get("status", "ok" if ok else "failed")
    if status not in STATUSES or (status == "ok") != ok:
        raise ProtocolError(f"status {status!r} does not match ok={ok}")
    error = resp.get("error")
    job_id = resp.get("job_id")
    if error is not None and not isinstance(error, str):
        raise ProtocolError("error must be a string")
    if job_id is not None and not isinstance(job_id, str):
        raise ProtocolError("job_id must be a string")
    return Outcome(status=status, result=result, error=error, job_id=job_id)


@dataclass
class Call:
    code: int | None
    stdout: str
    stderr_tail: list[str]
    stopped: str | None = None  # "timeout" or "ctrl-c"


def _stop(proc: subprocess.Popen[str], grace: float) -> None:
    try:
        if grace > 0:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                pass
        proc.kill()
        proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()


def call(conn: Connector, action: str, request: dict[str, Any], *, timeout: float, grace: float, cwd: str | None = None) -> Call:
    proc = subprocess.Popen(
        [*conn.command, action],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=cwd,
    )
    out: list[str] = []
    tail: deque[str] = deque(maxlen=STDERR_TAIL)

    def pump_err() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            sys.stderr.write(f"[{conn.name}] {line}" if line.endswith("\n") else f"[{conn.name}] {line}\n")
            sys.stderr.flush()
            tail.append(line.rstrip("\n"))

    readers = [
        threading.Thread(target=lambda: out.append(proc.stdout.read()), daemon=True),  # type: ignore[union-attr]
        threading.Thread(target=pump_err, daemon=True),
    ]
    for reader in readers:
        reader.start()
    assert proc.stdin is not None
    try:
        proc.stdin.write(json.dumps(request))
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    stopped = None
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stopped = "timeout"
        _stop(proc, grace)
    except KeyboardInterrupt:
        stopped = "ctrl-c"
        _stop(proc, grace)
    for reader in readers:
        reader.join(timeout=5)
    return Call(code=proc.returncode, stdout="".join(out), stderr_tail=list(tail), stopped=stopped)


def _decode(stdout: str) -> Any:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"stdout is not valid JSON ({exc.msg})") from exc


def _with_tail(msg: str, tail: list[str]) -> str:
    return msg if not tail else f"{msg}; stderr tail: {tail[-1]}"


def ask_questions(conn: Connector, request: dict[str, Any]) -> Questions:
    got = call(conn, "questions", request, timeout=QUESTIONS_TIMEOUT, grace=0, cwd=request.get("repo_root"))
    if got.stopped == "ctrl-c":
        raise KeyboardInterrupt
    if got.stopped == "timeout":
        raise ConnectorError(f"questions timed out after {QUESTIONS_TIMEOUT:g}s")
    if got.code != 0:
        raise ConnectorError(_with_tail(f"questions exited {got.code}", got.stderr_tail))
    try:
        return parse_questions(_decode(got.stdout))
    except ProtocolError as exc:
        raise ConnectorError(_with_tail(f"invalid questions response: {exc}", got.stderr_tail)) from exc


def run_connector(conn: Connector, request: dict[str, Any], timeout: float) -> Outcome:
    got = call(conn, "run", request, timeout=timeout, grace=STOP_GRACE, cwd=request.get("repo_root"))
    parsed: Outcome | None = None
    reason: str | None = None
    try:
        parsed = parse_run(_decode(got.stdout))
    except ProtocolError as exc:
        reason = f"invalid run response: {exc}"
    if got.stopped:
        why = f"timed out after {timeout:g}s" if got.stopped == "timeout" else "interrupted by Ctrl-C"
        outcome = parsed if parsed and parsed.status == "interrupted" else Outcome(status="interrupted", result=parsed.result if parsed else {})
        outcome.error = outcome.error or why
        outcome.ctrl_c = got.stopped == "ctrl-c"
        return outcome
    if got.code != 0:
        if parsed and parsed.status == "interrupted":
            return parsed
        return Outcome(status="failed", error=_with_tail(f"run exited {got.code}", got.stderr_tail))
    if parsed is None:
        return Outcome(status="failed", error=_with_tail(reason or "invalid run response", got.stderr_tail))
    return parsed


# ---------- answers ----------


class MissingAnswer(ConnectorError):
    pass


def _matches(when: dict[str, Any], answers: dict[str, Any]) -> bool:
    for key, expected in when.items():
        if key not in answers:
            return False
        value = answers[key]
        if isinstance(expected, list):
            if value not in expected:
                return False
        elif value != expected:
            return False
    return True


def describe(q: Question) -> str:
    extra = ""
    if q.when:
        extra += f" (only if {json.dumps(q.when)})"
    if q.default_when:
        extra += " (default depends on earlier answers)"
    if q.persist == "project":
        extra += " (stored in .release)"
    if q.type == "bool":
        return f"{q.prompt} (y/n) [{'y' if q.default else 'n'}]{extra}"
    if q.type == "choice":
        return f"{q.prompt} ({'/'.join(q.options)}) [{q.default}]{extra}"
    kind = q.type if q.type == "text" else f"int {q.lo if q.lo is not None else ''}..{q.hi if q.hi is not None else ''}"
    if q.pattern:
        kind += f" matching {q.pattern}"
    default = f", default {q.default}" if q.default is not None else ""
    return f"{q.prompt} [{kind}{default}]{extra}"


def _missing(conn: Connector, q: Question, why: str) -> MissingAnswer:
    if q.persist == "project":
        return MissingAnswer(
            f"{why} for {conn.name}.{q.id} and --defaults does not invent one; "
            f"set it with: release edit config set {conn.name} {q.id} <value>"
        )
    return MissingAnswer(f"{why} for {conn.name}.{q.id}; run without --defaults to answer it")


def _ask_one(conn: Connector, q: Question, default: Any, *, defaults: bool, ask: prompts.Ask) -> Any:
    """The answer, or None when an optional text/int was left empty."""
    label = f"[{conn.name}] {q.prompt}"
    if q.type == "bool":
        return prompts.ask_bool(label, default, defaults=defaults, ask=ask)
    if q.type == "choice":
        return prompts.ask_choice(label, list(q.options), default, defaults=defaults, ask=ask)
    if q.type == "int":
        check = lambda raw: prompts.int_error(raw, q.lo, q.hi)  # noqa: E731
    else:
        check = lambda raw: q.value_error(raw)  # noqa: E731
    try:
        raw = prompts.ask_text(
            label, None if default is None else str(default), required=q.required, defaults=defaults, ask=ask, validate=check
        )
    except prompts.PromptError as exc:
        raise _missing(conn, q, "no value") from exc
    if q.type == "int":
        return int(raw) if raw else None
    return raw if raw or q.type == "text" else None


def collect_answers(
    conn: Connector,
    items: list[Question],
    *,
    cwd: Path,
    project_values: dict[str, Any],
    defaults: bool,
    ask: prompts.Ask,
) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for q in items:
        if not _matches(q.when, answers):
            continue
        if q.persist == "project" and q.id in project_values:
            stored = project_values[q.id]
            err = q.value_error(stored)
            if err is None:
                answers[q.id] = stored
                continue
            if defaults:
                raise _missing(conn, q, f"stored value {stored!r} is invalid ({err})")
            print(f"  stored {conn.name}.{q.id} = {stored!r} is invalid ({err}); asking again")
        value = _ask_one(conn, q, q.default_for(answers), defaults=defaults, ask=ask)
        if value is None:
            continue
        if q.persist == "project":
            set_connector_value(cwd, conn.name, q.id, value)
            project_values[q.id] = value
        answers[q.id] = value
    return answers


# ---------- context ----------


def repo_name(remote: str, root: Path) -> str:
    base = remote.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    base = base[:-4] if base.endswith(".git") else base
    return base or root.name


def build_context(root: Path, remote: str, artifact: str, release_version: str, sha: str) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "repo": repo_name(remote, root),
        "remote_url": remote,
        "repo_root": str(root),
        "artifact": artifact,
        "release_version": release_version,
        "tags": [release_version, f"{artifact}-{release_version}"],
        "sha": sha,
        "mode": "rc" if "-rc" in release_version else "fv",
    }


def new_state(context: dict[str, Any]) -> dict[str, Any]:
    return {**{k: v for k, v in context.items() if k != "protocol"}, "connectors": {}}


# ---------- run state ----------


def runs_dir() -> Path:
    return data_dir() / "runs"


def state_path(repo: str, tag: str) -> Path:
    return runs_dir() / repo / f"{tag}.json"


def load_state(repo: str, tag: str) -> dict[str, Any] | None:
    path = state_path(repo, tag)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> Path:
    path = state_path(state["repo"], state["release_version"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def resume_hint(tag: str, name: str) -> str:
    return f"release connectors run {tag} --from {name}"


# ---------- chain ----------


def run_chain(
    context: dict[str, Any],
    conns: list[Connector],
    *,
    state: dict[str, Any],
    prior: dict[str, Any],
    cwd: Path,
    project: dict[str, Any],
    defaults: bool,
    dry_run: bool,
    log: Callable[[str], None],
    ask: prompts.Ask = input,
) -> bool:
    """Run enabled connectors in order. Returns False if any failed. Never touches git."""
    tag = context["release_version"]
    saved: dict[str, Any] = state.setdefault("connectors", {})
    all_ok = True
    for conn in conns:
        if not conn.enabled:
            continue
        previous = saved.get(conn.name) or {}
        resume_job = previous.get("job_id") if previous.get("status") == "interrupted" else None
        project_values = dict(project.get(conn.name, {}))
        request = {
            **context,
            "protocol": PROTOCOL,
            "connector": conn.name,
            "dry_run": dry_run,
            "prior": prior,
            "config": dict(conn.config),
            "resume_job_id": resume_job,
        }
        log(f"CONNECTOR {conn.name}" + (f" (resuming job {resume_job})" if resume_job else ""))
        try:
            qs = ask_questions(conn, {**request, "action": "questions"})
            if qs.blocked and dry_run:
                log(f"  (dry-run: {conn.name} would be blocked: {qs.reason or 'prerequisite not met'})")
                continue
            if qs.blocked:
                raise ConnectorError(f"blocked: {qs.reason or 'a prerequisite failed (the connector gave no reason)'}")
            if not qs.applies:
                why = f": {qs.reason}" if qs.reason else " (the connector gave no reason)"
                log(f"skipping {conn.name}: does not apply to this project{why}")
                continue
            if dry_run:
                for q in qs.items:
                    log(f"  ? {describe(q)}")
                log(f"  (dry-run: {conn.name} run not called)")
                continue
            answers = collect_answers(conn, qs.items, cwd=cwd, project_values=project_values, defaults=defaults, ask=ask)
        except ConnectorError as exc:
            outcome = Outcome(status="failed", error=str(exc))
        else:
            timeout = conn.timeout_seconds or qs.timeout_seconds or DEFAULT_RUN_TIMEOUT
            request["config"] = {**conn.config, **project_values}
            outcome = run_connector(conn, {**request, "action": "run", "answers": answers}, timeout)
        if outcome.job_id is None and outcome.status == "interrupted" and resume_job:
            outcome.job_id = resume_job
        saved[conn.name] = {**outcome.entry(), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        save_state(state)
        prior[conn.name] = outcome.entry()
        if outcome.status == "ok":
            log(f"{conn.name}: ok")
            continue
        all_ok = False
        log(f"{conn.name}: {outcome.status}: {outcome.error or 'no reason given'}")
        if outcome.job_id:
            log(f"  job_id: {outcome.job_id}")
        log(f"  the tag {tag} stays pushed. resume: {resume_hint(tag, conn.name)}")
        if outcome.ctrl_c:
            raise ChainInterrupted(conn.name)
        if conn.break_on_error:
            log(f"  break_on_error: chain stopped after {conn.name}")
            break
    return all_ok
