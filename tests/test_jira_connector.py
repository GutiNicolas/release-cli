"""The `jira` connector against a fake Jira Cloud (local http.server). No real Jira, token or keychain."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from release_cli import jira_connector as jc
from release_cli.connectors import parse_questions, parse_run, save_release_toml

TOKEN = "tok-s3cret-for-tests"
WORKFLOW = {  # status -> transitions Jira offers from it
    "Code Review Approved": [{"id": "31", "name": "Entregado", "to": {"name": "Entregado"}}],
    "In Progress": [{"id": "21", "name": "Code Review", "to": {"name": "Code Review"}}],
    "Entregado": [],
}


class FakeJira:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.versions: list[dict[str, Any]] = []
        self.log: list[tuple[str, str]] = []
        self.reject_auth = False
        self.redirect: str | None = None
        self.html = False

    def add(self, key: str, status: str, summary: str = "does a thing") -> None:
        self.issues[key] = {"key": key, "fields": {"summary": summary, "status": {"name": status}, "fixVersions": []}}

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.log if m != "GET"]

    def handle(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        self.log.append((method, path.split("?")[0]))
        if self.reject_auth:
            return 401, {"errorMessages": ["unauthorized"]}
        if m := re.fullmatch(r"/rest/api/3/issue/([A-Z0-9-]+)", path.split("?")[0]):
            issue = self.issues.get(m[1])
            if issue is None:
                return 404, {"errorMessages": ["Issue does not exist"]}
            if method == "PUT":
                for op in body["update"]["fixVersions"]:
                    issue["fields"]["fixVersions"].append({"name": op["add"]["name"]})
                return 204, None
            return 200, issue
        if m := re.fullmatch(r"/rest/api/3/issue/([A-Z0-9-]+)/transitions", path):
            issue = self.issues[m[1]]
            if method == "POST":
                moved = next(t for t in WORKFLOW[issue["fields"]["status"]["name"]] if t["id"] == body["transition"]["id"])
                issue["fields"]["status"]["name"] = moved["to"]["name"]
                return 204, None
            return 200, {"transitions": WORKFLOW[issue["fields"]["status"]["name"]]}
        if path == "/rest/api/3/project/ABC/versions":
            return 200, self.versions
        if path == "/rest/api/3/version" and method == "POST":
            version = {"id": str(100 + len(self.versions)), "name": body["name"], "released": False}
            self.versions.append(version)
            return 201, version
        if m := re.fullmatch(r"/rest/api/3/version/(\d+)", path):
            next(v for v in self.versions if v["id"] == m[1]).update(released=body["released"])
            return 204, None
        return 404, {"errorMessages": [f"fake has no {method} {path}"]}


@pytest.fixture
def jira(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeJira]:
    fake = FakeJira()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if fake.html:  # a proxy or SSO page in front of Jira
                self.send_response(200)
                self.send_header("Content-Length", "15")
                self.end_headers()
                self.wfile.write(b"<html>hi</html>")
                return
            if fake.redirect:
                self.send_response(302)
                self.send_header("Location", fake.redirect)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status, payload = fake.handle(self.command, self.path, json.loads(raw) if raw else {})
            if status == 401:  # a hostile server may echo what it received
                payload["echo"] = self.headers.get("Authorization")
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = _serve

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    save_release_toml({"jira": {"base_url": f"http://127.0.0.1:{server.server_port}", "email": "dev@example.com"}})
    monkeypatch.setenv("RELEASE_SECRET_JIRA_TOKEN", TOKEN)
    yield fake
    server.shutdown()


def req(repo_root: Path, mode: str = "fv", version: str = "1.0.0", **extra: Any) -> dict[str, Any]:
    return {
        "protocol": 1,
        "repo": "example-app",
        "repo_root": str(repo_root),
        "artifact": "example-app",
        "release_version": version,
        "mode": mode,
        "config": {"project_key": "ABC", "delivered_transition": "Entregado"},
        **extra,
    }


def run_ok(resp: dict[str, Any]) -> dict[str, Any]:
    """The run response through the CLI's own parser; returns its result."""
    outcome = parse_run(resp)
    return {"ok": outcome.status == "ok", "error": outcome.error, **outcome.result}


def test_final_creates_assigns_delivers_then_releases(jira: FakeJira, tmp_path: Path) -> None:
    jira.add("ABC-1", "Code Review Approved")
    jira.add("ABC-2", "Entregado")  # delivered already: left alone
    result = run_ok(jc.run(req(tmp_path, answers={"issues": ["ABC-1", "ABC-2"]})))

    assert result["ok"] and result["released"] and result["jira_version"] == "example-app-1.0.0"
    assert {i["key"]: i["status"] for i in result["issues"]} == {"ABC-1": "delivered", "ABC-2": "already"}
    assert jira.issues["ABC-1"]["fields"]["status"]["name"] == "Entregado"
    assert [v["released"] for v in jira.versions] == [True]
    assert jira.writes() == [
        ("POST", "/rest/api/3/version"),
        ("PUT", "/rest/api/3/issue/ABC-1"),
        ("POST", "/rest/api/3/issue/ABC-1/transitions"),
        ("PUT", "/rest/api/3/issue/ABC-2"),
        ("PUT", "/rest/api/3/version/100"),  # released last, after every ticket is delivered
    ]


def test_undeliverable_ticket_fails_leaves_version_unreleased_and_resume_completes(jira: FakeJira, tmp_path: Path) -> None:
    jira.add("ABC-1", "Code Review Approved")
    jira.add("ABC-2", "In Progress")  # no way to Entregado from here
    first = run_ok(jc.run(req(tmp_path, answers={"issues": ["ABC-1", "ABC-2"]})))

    assert not first["ok"] and not first["released"]
    assert "ABC-2" in first["error"] and "release connectors run 1.0.0 --from jira" in first["error"]
    assert {i["key"]: i["status"] for i in first["issues"]} == {"ABC-1": "delivered", "ABC-2": "failed"}
    assert [v["released"] for v in jira.versions] == [False]

    jira.issues["ABC-2"]["fields"]["status"]["name"] = "Code Review Approved"  # fixed in Jira
    second = run_ok(jc.run(req(tmp_path, answers={"issues": ["ABC-1", "ABC-2"]})))

    assert second["ok"] and second["released"]
    assert {i["key"]: i["status"] for i in second["issues"]} == {"ABC-1": "already", "ABC-2": "delivered"}
    assert len(jira.versions) == 1  # the existing version is reused, never recreated or renamed
    assert [f["name"] for f in jira.issues["ABC-1"]["fields"]["fixVersions"]] == ["example-app-1.0.0"]


def test_final_without_tickets_fails_and_writes_nothing(jira: FakeJira, tmp_path: Path) -> None:
    result = run_ok(jc.run(req(tmp_path, answers={"issues": []})))
    assert not result["ok"] and "no tickets" in result["error"]
    assert jira.log == []


def test_rc_writes_nothing_and_warns_about_tickets_outside_qa_statuses(jira: FakeJira, tmp_path: Path) -> None:
    jira.add("ABC-1", "Code Review")
    jira.add("ABC-2", "In Progress")
    jira.add("ABC-3", "Code Review Approved")
    result = run_ok(jc.run(req(tmp_path, mode="rc", version="1.0.0-rc1", answers={"issues": ["ABC-1", "ABC-2", "ABC-3", "ABC-9"]})))

    assert result["ok"] and not result["released"] and result["jira_version"] == "example-app-1.0.0"
    assert {i["key"]: i["status"] for i in result["issues"]} == {"ABC-1": "ok", "ABC-2": "warned", "ABC-3": "ok", "ABC-9": "warned"}
    assert jira.writes() == []


def test_token_never_reaches_output_even_when_jira_echoes_it(jira: FakeJira, tmp_path: Path) -> None:
    jira.add("ABC-1", "Code Review Approved")
    ok = jc.run(req(tmp_path, answers={"issues": ["ABC-1"]}))
    jira.reject_auth = True
    rejected = jc.run(req(tmp_path, answers={"issues": ["ABC-1"]}))
    asked = jc.questions(req(tmp_path))

    assert ok["ok"] and not rejected["ok"]
    basic = jc.Jira("http://x", "dev@example.com", TOKEN)._auth
    for resp in (ok, rejected, asked):
        text = json.dumps(resp)
        assert TOKEN not in text and basic not in text and basic.split()[1] not in text


@pytest.mark.parametrize(("mode", "version", "blocked"), [("rc", "1.0.0-rc1", False), ("fv", "1.0.0", True)])
def test_not_configured_skips_an_rc_and_blocks_a_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, version: str, blocked: bool) -> None:
    # A final must not reach the deploy with the Jira version unreleased because the token is missing on this machine.
    monkeypatch.setattr(jc.secrets, "get", lambda name: None)  # never the real keychain
    resp = parse_questions(jc.questions(req(tmp_path, mode=mode, version=version)))
    assert not resp.applies and resp.blocked is blocked and "release secret set jira-token" in (resp.reason or "")


def test_token_is_not_sent_to_a_redirect_target(jira: FakeJira, tmp_path: Path) -> None:
    # urllib copies the Authorization header onto a redirect, to any host and over plain http.
    seen: list[str | None] = []

    class Elsewhere(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"[]")

        def log_message(self, *args: Any) -> None:
            pass

    other = HTTPServer(("127.0.0.1", 0), Elsewhere)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    jira.redirect = f"http://127.0.0.1:{other.server_port}/steal"
    jira.add("ABC-1", "Code Review Approved")
    result = run_ok(jc.run(req(tmp_path, answers={"issues": ["ABC-1"]})))
    other.shutdown()

    assert not result["ok"] and seen == []


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = T\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init")
    for i, msg in enumerate(["ABC-1 old work", "[manual-release] 1.0.0", "ABC-2 login (#4)", "XYZ-7 other team\n\nrefs ABC-3", "[manual-release] 1.1.0-rc1"]):
        (root / "f.txt").write_text(str(i))
        git("add", "-A")
        git("commit", "-m", msg)
        if i == 1:
            git("tag", "-a", "1.0.0", "-m", "final")
    git("tag", "-a", "1.1.0-rc1", "-m", "rc")
    git("tag", "-a", "1.1.0", "-m", "final")
    return root


def test_questions_offer_the_commits_since_previous_final_with_only_the_projects_keys_ticked(jira: FakeJira, repo: Path) -> None:
    jira.add("ABC-2", "Code Review Approved", "Login")
    jira.add("ABC-3", "In Progress", "Fix logout")
    jira.add("XYZ-7", "Code Review", "Other thing")
    parsed = parse_questions(jc.questions(req(repo, version="1.1.0")))

    issues = next(q for q in parsed.items if q.id == "issues")
    assert issues.options == ("ABC-2", "ABC-3", "XYZ-7")  # ABC-1 shipped in 1.0.0
    assert set(issues.default) == {"ABC-2", "ABC-3"}
    assert issues.labels[0] == "ABC-2 Login (Code Review Approved)"
    assert {q.id for q in parsed.items} == {"issues"}  # project and transition already stored


def test_first_time_asks_project_and_transition_and_ticks_by_the_answered_project(jira: FakeJira, repo: Path) -> None:
    jira.add("ABC-2", "Code Review Approved")
    jira.add("ABC-3", "Code Review Approved")
    jira.add("XYZ-7", "Code Review Approved")
    parsed = parse_questions(jc.questions({**req(repo, version="1.1.0"), "config": {}}))

    assert [q.id for q in parsed.items] == ["project_key", "delivered_transition", "issues"]
    transition = parsed.items[1]
    assert transition.options == ("Entregado",) and transition.default == "Entregado"
    issues = parsed.items[2]
    assert issues.default_for({"project_key": "ABC"}) == ["ABC-2", "ABC-3"]
    assert issues.default_for({"project_key": "XYZ"}) == ["XYZ-7"]


def test_non_json_answer_skips_an_rc_instead_of_crashing(jira: FakeJira, repo: Path) -> None:
    jira.html = True
    resp = parse_questions(jc.questions(req(repo, mode="rc", version="1.1.0-rc1")))
    assert not resp.applies and not resp.blocked and "not JSON" in (resp.reason or "")
