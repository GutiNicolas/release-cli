"""Built-in connector `jira`: before a final is deployed, make Jira say it is delivered.

On a final: create the Jira version `{artifact}-{X.Y.Z}` if missing, set it as fixVersion of the reviewed
tickets, move each ticket to the delivered status with the project's transition, then release the version.
On an RC it writes nothing and only warns about tickets that are not in a QA status. Jira Cloud only
(REST v3, email + API token). The token comes from `release_cli.secrets` and is never part of a result.

    python -m release_cli.jira_connector questions|run   (protocol 1 on stdin/stdout)
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlparse

from release_cli import github_release, secrets
from release_cli.connectors import load_release_toml, resume_hint
from release_cli.tagformat import scheme_of, tag_of

TOKEN_SECRET = "jira-token"
KEY_RE = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
PROJECT_RE = "[A-Z][A-Z0-9_]+"
DEFAULT_QA_STATUSES = ["Code Review", "Code Review Approved"]
DELIVERED_HINTS = ("delivered", "entregado")  # default transition: the one leading to a status named like this
HTTP_TIMEOUT = 15.0
LOOKUP_WORKERS = 8
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
NOT_CONFIGURED = (
    "Jira is not configured: set `[jira] base_url` and `email` in ~/.config/release/release.toml "
    "and store the API token with `release secret set jira-token`"
)


class JiraError(RuntimeError):
    """A Jira call failed. `status` is the HTTP status, 0 for a network error."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """urllib copies the Authorization header onto a redirect (any host, even plain http): refuse redirects."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urlrequest.build_opener(_NoRedirect)


class Jira:
    def __init__(self, base_url: str, email: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urlrequest.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Authorization": self._auth, "Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with _OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (scheme checked in `client`)
                raw = resp.read()
        except urlerror.HTTPError as exc:
            detail = self._clean(exc.read().decode("utf-8", "replace").strip())  # redact before cutting
            raise JiraError(f"{method} {path}: HTTP {exc.code} {detail[:300]}", exc.code) from None
        except (urlerror.URLError, OSError, TimeoutError, http.client.HTTPException) as exc:
            raise JiraError(self._clean(f"{method} {path}: {getattr(exc, 'reason', exc)}")) from None
        try:
            return json.loads(raw) if raw.strip() else None
        except ValueError:
            raise JiraError(f"{method} {path}: the answer is not JSON (a proxy or login page in front of Jira?)") from None

    def _clean(self, text: str) -> str:
        return text.replace(self._token, "***").replace(self._auth.split()[1], "***")  # the header, or its value alone

    def issue(self, key: str) -> dict[str, Any]:
        return self.call("GET", f"/rest/api/3/issue/{quote(key)}?fields=summary,status,fixVersions")

    def transitions(self, key: str) -> list[dict[str, Any]]:
        return self.call("GET", f"/rest/api/3/issue/{quote(key)}/transitions")["transitions"]


def client(config: dict[str, Any]) -> Jira | str:
    """A Jira client, or the reason there is none (not configured, unsafe URL)."""
    toml = load_release_toml().get("jira")
    toml = toml if isinstance(toml, dict) else {}
    base_url = str(toml.get("base_url") or config.get("base_url") or "").strip()
    email = str(toml.get("email") or config.get("email") or "").strip()
    try:
        token = secrets.get(TOKEN_SECRET)
    except secrets.SecretError:
        token = None
    if not (base_url and email and token):
        return NOT_CONFIGURED
    parsed = urlparse(base_url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS):
        return "`[jira] base_url` must be an https:// URL"
    return Jira(base_url, email, token)


# ---------- tickets ----------


def jira_version_name(req: dict[str, Any]) -> str:
    base = str(req["release_version"]).split("-rc")[0]
    return f"{req['artifact']}-{base}"


def commit_keys(req: dict[str, Any]) -> list[str]:
    """Ticket keys in the commits this version ships (same range as `release notes`), sorted."""
    root = Path(req["repo_root"])
    tag = tag_of(req)
    tagged = subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], cwd=root, capture_output=True, check=False)
    _prev, rng = github_release.commit_range(root, tag, end=None if tagged.returncode == 0 else "HEAD", scheme=scheme_of(req))
    keys = set(KEY_RE.findall(github_release.git_log(root, rng, "%B", merges=True)))
    return sorted(keys, key=lambda k: (k.rsplit("-", 1)[0], int(k.rsplit("-", 1)[1])))


def _prefix(key: str) -> str:
    return key.rsplit("-", 1)[0]


def _status_name(issue: dict[str, Any]) -> str:
    return str(((issue.get("fields") or {}).get("status") or {}).get("name") or "")


def _lookup(jira: Jira, keys: list[str]) -> dict[str, dict[str, Any] | None]:
    def one(key: str) -> dict[str, Any] | None:
        try:
            return jira.issue(key)
        except JiraError as exc:
            if exc.status in (400, 404):
                return None  # not a ticket (UTF-8, SHA-256...): listed as "not found"
            raise

    with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
        return dict(zip(keys, pool.map(one, keys), strict=True))


def _transition_options(jira: Jira, keys: list[str]) -> tuple[list[str], str | None]:
    """Transition names Jira offers from a few of `keys`, and the one leading to a delivered-looking status."""
    names: list[str] = []
    guess: str | None = None
    for key in keys[:5]:
        try:
            found = jira.transitions(key)
        except JiraError:
            continue
        for t in found:
            name = str(t.get("name") or "")
            if name and name not in names:
                names.append(name)
            if name and guess is None and any(h in str((t.get("to") or {}).get("name") or "").lower() for h in DELIVERED_HINTS):
                guess = name
    return names, guess


# ---------- questions ----------


def questions(req: dict[str, Any]) -> dict[str, Any]:
    config = req.get("config") or {}
    final = req.get("mode") == "fv"
    jira = client(config)
    if isinstance(jira, str):  # a final must not reach the deploy with its Jira version unreleased
        return {"protocol": 1, "applies": False, "blocked": final, "reason": jira, "questions": []}
    try:
        keys = commit_keys(req)
        info = _lookup(jira, keys)
    except github_release.NotesError as exc:
        return {"protocol": 1, "applies": False, "blocked": final, "reason": f"could not read the commits: {exc}", "questions": []}
    except JiraError as exc:
        return {"protocol": 1, "applies": False, "blocked": final, "reason": f"Jira refused the request: {exc}", "questions": []}

    def label(key: str) -> str:
        issue = info.get(key)
        if issue is None:
            return f"{key} (not found in Jira)"
        summary = str((issue.get("fields") or {}).get("summary") or "").strip()
        return " ".join(part for part in (key, summary, f"({_status_name(issue) or 'no status'})") if part)

    stored = str(config.get("project_key") or "").strip()
    out: list[dict[str, Any]] = []
    if not stored:
        project_q: dict[str, Any] = {
            "id": "project_key",
            "type": "text",
            "prompt": "Jira project key of this repository's tickets (e.g. ABC)",
            "pattern": PROJECT_RE,
            "persist": "project",
        }
        if config.get("jira_project_key"):  # the deploy connector's key, when the CLI passes it
            project_q["default"] = str(config["jira_project_key"])
        out.append(project_q)
    if final and not config.get("delivered_transition"):
        sample = [k for k in keys if not stored or _prefix(k) == stored] or keys
        names, guess = _transition_options(jira, sample)
        prompt = "Transition that moves a ticket to the delivered status"
        if names:
            q: dict[str, Any] = {"id": "delivered_transition", "type": "choice", "prompt": prompt, "options": names, "persist": "project"}
            if guess:
                q["default"] = guess
        else:
            q = {"id": "delivered_transition", "type": "text", "prompt": prompt + " (exact name in your workflow)", "persist": "project"}
        out.append(q)
    multi: dict[str, Any] = {
        "id": "issues",
        "type": "multi",
        "prompt": f"Tickets shipped in {req['release_version']}" + (" (they are delivered in Jira)" if final else " (checked against the QA statuses)"),
        "options": [{"value": k, "label": label(k)} for k in keys],
        "default": [k for k in keys if _prefix(k) == stored] if stored else [],
        "other": {"pattern": "[A-Z][A-Z0-9_]+-[0-9]+"},
    }
    if not stored:  # the project key is asked just before: tick the keys of whichever project it names
        multi["default_when"] = [
            {"if": {"project_key": p}, "default": [k for k in keys if _prefix(k) == p]} for p in sorted({_prefix(k) for k in keys})
        ]
    out.append(multi)
    return {"protocol": 1, "questions": out}


# ---------- run ----------


def _fail(error: str, **result: Any) -> dict[str, Any]:
    return {"protocol": 1, "ok": False, "error": error, "result": result}


def _entry(key: str, issue: dict[str, Any] | None, status: str, reason: str = "", to: str = "") -> dict[str, Any]:
    fields = (issue or {}).get("fields") or {}
    return {"key": key, "summary": str(fields.get("summary") or ""), "from": _status_name(issue or {}), "to": to, "status": status, "reason": reason}


def run_rc(jira: Jira, req: dict[str, Any], keys: list[str], qa: list[str]) -> dict[str, Any]:
    allowed = {s.lower() for s in qa}
    issues = []
    for key in keys:
        try:
            issue = jira.issue(key)
        except JiraError as exc:
            issues.append(_entry(key, None, "warned", str(exc)))
            continue
        if _status_name(issue).lower() in allowed:
            issues.append(_entry(key, issue, "ok"))
        else:
            reason = f"status {_status_name(issue) or 'unknown'!r} is not one of: {', '.join(qa)}"
            issues.append(_entry(key, issue, "warned", reason))
            print(f"warning: {key}: {reason}", file=sys.stderr)
    return {"protocol": 1, "ok": True, "result": {"mode": "rc", "jira_version": jira_version_name(req), "released": False, "url": "", "issues": issues}}


def _ensure_version(jira: Jira, project: str, name: str) -> dict[str, Any]:
    for v in jira.call("GET", f"/rest/api/3/project/{quote(project)}/versions") or []:
        if v.get("name") == name:  # exact match; an existing version is never renamed
            return v
    return jira.call("POST", "/rest/api/3/version", {"name": name, "project": project})


def _deliver(jira: Jira, key: str, version: str, transition: str, target: list[str]) -> dict[str, Any]:
    """Set the fixVersion and move one ticket to the delivered status. Never raises for a Jira error."""
    issue: dict[str, Any] | None = None
    try:
        issue = jira.issue(key)
        if version not in [v.get("name") for v in (issue.get("fields") or {}).get("fixVersions") or []]:
            jira.call("PUT", f"/rest/api/3/issue/{quote(key)}", {"update": {"fixVersions": [{"add": {"name": version}}]}})
        current = _status_name(issue)
        found = [t for t in jira.transitions(key) if str(t.get("name") or "").lower() == transition.lower()]
        if found:
            target[:] = [str((found[0].get("to") or {}).get("name") or transition)]
        if current.lower() in {t.lower() for t in [*target, transition]}:
            return _entry(key, issue, "already", to=current)
        if not found:
            return _entry(key, issue, "failed", f"no transition {transition!r} from status {current or 'unknown'!r}")
        jira.call("POST", f"/rest/api/3/issue/{quote(key)}/transitions", {"transition": {"id": found[0]["id"]}})
        return _entry(key, issue, "delivered", to=target[0] if target else transition)
    except JiraError as exc:
        return _entry(key, issue, "failed", str(exc))


def run_final(jira: Jira, req: dict[str, Any], keys: list[str], project: str, transition: str) -> dict[str, Any]:
    name = jira_version_name(req)
    tag = tag_of(req)
    try:
        version = _ensure_version(jira, project, name)
    except JiraError as exc:
        return _fail(f"could not create or read the Jira version {name}: {exc}", mode="fv", jira_version=name, released=False, issues=[])
    url = f"{jira.base_url}/projects/{project}/versions/{version['id']}"
    target: list[str] = []  # name of the delivered status, learned from the first ticket that offers the transition
    issues = [_deliver(jira, k, name, transition, target) for k in keys]
    for i in issues:  # a ticket seen before any other ticket taught the delivered status: judge it again
        if i["status"] == "failed" and i["reason"].startswith("no transition") and target and i["from"].lower() == target[0].lower():
            i.update(status="already", reason="", to=i["from"])
    failed = [i for i in issues if i["status"] == "failed"]
    result: dict[str, Any] = {"mode": "fv", "jira_version": name, "released": bool(version.get("released")), "url": url, "issues": issues}
    if failed:
        listing = "; ".join(f"{i['key']}: {i['reason']}" for i in failed)
        return _fail(
            f"{len(failed)} ticket(s) not delivered, version {name} left unreleased: {listing}. "
            f"Fix them in Jira (or untick them), then: {resume_hint(tag, 'jira')}",
            **result,
        )
    if not version.get("released"):
        try:
            jira.call("PUT", f"/rest/api/3/version/{version['id']}", {"released": True, "releaseDate": date.today().isoformat()})
        except JiraError as exc:
            return _fail(f"all tickets are delivered but releasing {name} failed: {exc}. Resume: {resume_hint(tag, 'jira')}", **result)
        result["released"] = True
    print(f"Jira version {name} released with {len(keys)} ticket(s)", file=sys.stderr)
    return {"protocol": 1, "ok": True, "result": result}


def run(req: dict[str, Any]) -> dict[str, Any]:
    config = req.get("config") or {}
    answers = req.get("answers") or {}
    final = req.get("mode") == "fv"
    keys = [str(k) for k in answers.get("issues") or []]
    jira = client(config)
    if isinstance(jira, str):
        if final:
            return _fail(jira, mode="fv", jira_version=jira_version_name(req), released=False, issues=[])
        return {"protocol": 1, "ok": True, "result": {"mode": "rc", "jira_version": jira_version_name(req), "released": False, "url": "", "issues": [], "reason": jira}}
    if not final:
        qa = config.get("qa_statuses")
        return run_rc(jira, req, keys, [str(s) for s in qa] if isinstance(qa, list) and qa else DEFAULT_QA_STATUSES)
    if not keys:
        return _fail("no tickets selected: Jira cannot release a version without tickets", mode="fv", jira_version=jira_version_name(req), released=False, issues=[])
    project = str(answers.get("project_key") or config.get("project_key") or "")
    transition = str(answers.get("delivered_transition") or config.get("delivered_transition") or "")
    if not (project and transition):
        return _fail("project_key and delivered_transition are not set: release edit config set jira project_key <KEY>", mode="fv", jira_version=jira_version_name(req), released=False, issues=[])
    return run_final(jira, req, keys, project, transition)


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
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
