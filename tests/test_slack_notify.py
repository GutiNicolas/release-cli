from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from release_cli import secrets
from release_cli import slack_notify as sn
from release_cli.connectors import BUILTINS, parse_questions, parse_run

SECRET = "slack-test"


class Hook:
    """A local fake incoming webhook: records every POST, answers with `status`."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.status = 200
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                hook.posts.append(json.loads(body))
                self.send_response(hook.status)
                self.end_headers()
                self.wfile.write(b"ok" if hook.status == 200 else b"no_service")

            def log_message(self, *args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/services/T000/B000/SECRETPART"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def hook(monkeypatch: pytest.MonkeyPatch) -> Iterator[Hook]:
    h = Hook()
    monkeypatch.setenv(secrets.env_var(SECRET), h.url)
    monkeypatch.setattr(secrets, "_has", lambda tool: False)  # never touch a real keychain
    yield h
    h.server.shutdown()
    h.server.server_close()


DEPLOY_OK = {"status": "ok", "result": {"status": "SUCCESS", "environment": "prd", "deploy_type": "full"}}
GITHUB_OK = {"status": "ok", "result": {"published": True, "url": "https://github.com/o/r/releases/tag/1.2.0"}}
JIRA_OK = {
    "status": "ok",
    "result": {
        "mode": "fv",
        "jira_version": "app-1.2.0",
        "released": True,
        "url": "https://example.atlassian.net/projects/ABC/versions/10",
        "issues": [
            {"key": "ABC-1", "summary": "Fix <login> & logout", "status": "delivered"},
            {"key": "ABC-2", "summary": "Already there", "status": "already"},
            {"key": "ABC-3", "summary": "Broken", "status": "failed"},
        ],
    },
}


def _req(version: str = "1.2.0", *, answers: dict | None = None, prior: dict | None = None, config: dict | None = None) -> dict:
    return {
        "protocol": 1,
        "action": "run",
        "repo": "demo",
        "release_version": version,
        "mode": "rc" if "-rc" in version else "fv",
        "answers": {"webhook_secret": SECRET, "notify_on": "final", "send": True, **(answers or {})},
        "prior": {"platform-deploy": DEPLOY_OK, "github-release": GITHUB_OK, "jira": JIRA_OK, **(prior or {})},
        "config": config or {},
    }


def _text(payload: dict) -> str:
    return json.dumps(payload)


def test_final_message_carries_deploy_release_link_and_delivered_tickets(hook: Hook, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sn, "release_notes", lambda root, tag, scheme=None: "## Changes since 1.1.0\n\n- add <login> (abc1234)\n")
    out = parse_run(sn.run({**_req(), "repo_root": "/repo"}))
    assert out.status == "ok" and out.result == {"sent": True}
    (payload,) = hook.posts
    blocks = payload["blocks"]
    assert blocks[0]["text"]["text"] == "demo 1.2.0"
    fields = [f["text"] for f in blocks[1]["fields"]]
    assert "*Environment*\nprd" in fields and "*Deploy type*\nfull" in fields
    assert "*Release*\n<https://github.com/o/r/releases/tag/1.2.0|GitHub Release>" in fields
    jira = blocks[2]["text"]["text"]
    assert "app-1.2.0" in jira and "(released)" in jira
    assert "• ABC-1 Fix &lt;login&gt; &amp; logout" in jira and "ABC-2" in jira and "ABC-3" not in jira
    assert "- add &lt;login&gt; (abc1234)" in blocks[3]["text"]["text"]


def test_long_notes_and_ticket_lists_stay_inside_slack_limits(hook: Hook, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sn, "release_notes", lambda root, tag, scheme=None: "\n".join(f"- change number {i} " + "x" * 40 for i in range(200)))
    many = {"status": "ok", "result": {**JIRA_OK["result"], "issues": [{"key": f"ABC-{i}", "summary": "s", "status": "delivered"} for i in range(60)]}}
    assert sn.run({**_req(prior={"jira": many}), "repo_root": "/repo"})["ok"]
    blocks = hook.posts[0]["blocks"]
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if "text" in b and b["type"] == "section")
    assert blocks[2]["text"]["text"].endswith("…and 40 more")
    assert blocks[3]["text"]["text"].endswith("…") and len(blocks[3]["text"]["text"]) <= sn.NOTES_LIMIT


def test_no_deploy_connector_notifies_right_after_the_push(hook: Hook) -> None:
    req = _req()
    req["prior"] = {}
    assert sn.run(req)["result"] == {"sent": True}
    assert "Environment" not in _text(hook.posts[0])


@pytest.mark.parametrize(
    ("version", "answers", "deploy"),
    [
        ("1.2.0-rc1", {}, DEPLOY_OK),  # notify_on=final: the send question is not even asked for an RC
        ("1.2.0", {"send": False}, DEPLOY_OK),
        ("1.2.0", {}, {"status": "failed", "result": {}, "error": "boom"}),
        ("1.2.0", {}, {"status": "ok", "result": {"status": "FAILURE"}}),
    ],
)
def test_nothing_is_sent_when_filtered_declined_or_deploy_did_not_succeed(hook: Hook, version: str, answers: dict, deploy: dict) -> None:
    req = _req(version, answers=answers, prior={"platform-deploy": deploy})
    if "-rc" in version:
        del req["answers"]["send"]
    res = sn.run(req)
    assert res["ok"] is True and res["result"]["sent"] is False and res["result"]["reason"]
    assert hook.posts == []


def test_every_deploy_announces_release_candidates_without_jira_section(hook: Hook) -> None:
    rc_jira = {"status": "ok", "result": {"mode": "rc", "jira_version": "app-1.2.0", "issues": [{"key": "ABC-1", "summary": "x", "status": "ok"}]}}
    res = sn.run(_req("1.2.0-rc1", answers={"notify_on": "every-deploy"}, prior={"jira": rc_jira}))
    assert res["result"] == {"sent": True}
    assert "1.2.0-rc1" in hook.posts[0]["text"] and "Jira version" not in _text(hook.posts[0])


def test_missing_secret_fails_with_the_command_to_fix_it(hook: Hook, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(secrets.env_var(SECRET))
    res = sn.run(_req())
    assert res["ok"] is False and f"release secret set {SECRET}" in res["error"]
    assert hook.posts == []


def test_webhook_url_never_appears_in_results_or_errors(hook: Hook, capsys: pytest.CaptureFixture[str]) -> None:
    ok = sn.run(_req())
    hook.status = 500
    failed = sn.run(_req())
    assert failed["ok"] is False and "HTTP 500" in failed["error"]
    seen = json.dumps([ok, failed]) + capsys.readouterr().err
    assert "SECRETPART" not in seen and hook.url not in seen

    hook.server.shutdown()
    hook.server.server_close()
    down = sn.run(_req())
    assert down["ok"] is False and "SECRETPART" not in json.dumps(down) and hook.url not in json.dumps(down)


def test_plain_http_to_a_remote_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(secrets.env_var(SECRET), "http://hooks.example.com/services/x")
    monkeypatch.setattr(secrets, "_has", lambda tool: False)
    res = sn.run(_req())
    assert res["ok"] is False and "https" in res["error"] and "hooks.example.com" not in res["error"]


def test_questions_persist_the_two_settings_and_ask_rc_notice_only_for_every_deploy() -> None:
    final = parse_questions(sn.questions({"release_version": "1.2.0", "mode": "fv"}))
    assert [(q.id, q.persist) for q in final.items] == [("webhook_secret", "project"), ("notify_on", "project"), ("send", "none")]
    assert final.items[1].default == "final" and final.items[2].default is True and final.items[2].when == {}
    rc = parse_questions(sn.questions({"release_version": "1.2.0-rc1", "mode": "rc"}))
    assert rc.items[2].when == {"notify_on": "every-deploy"}


def test_builtin_is_wired_and_speaks_protocol_1_over_stdin() -> None:
    assert BUILTINS["slack-notify"] == "release_cli.slack_notify"
    proc = subprocess.run(
        [sys.executable, "-m", "release_cli.slack_notify", "questions"],
        input=json.dumps({"action": "questions", "release_version": "1.2.0", "mode": "fv"}),
        capture_output=True,
        text=True,
        check=True,
    )
    assert parse_questions(json.loads(proc.stdout)).applies


def test_a_malformed_webhook_fails_without_showing_its_path(hook: Hook, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # http.client rejects a space in the path with an error that quotes the path, which is the secret part.
    monkeypatch.setenv(secrets.env_var(SECRET), hook.url + " SECRETPART2")
    res = sn.run(_req())
    assert res["ok"] is False
    assert "SECRETPART" not in json.dumps(res) + capsys.readouterr().err
