"""Built-in connector `slack-notify`: one Slack message per release that reached its target.

Posts Block Kit JSON to an incoming webhook (stdlib only). The webhook URL is a secret: config holds only its
*name* (`webhook_secret`), `release_cli.secrets.get` resolves it at run time, and the URL never reaches stdout,
the run state or an error message.

    python -m release_cli.slack_notify questions|run   (protocol 1 on stdin/stdout)
"""

from __future__ import annotations

import http.client
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from release_cli import secrets
from release_cli.github_release import NotesError, release_notes
from release_cli.tagformat import scheme_of, tag_of

NOTIFY_ON = ("final", "every-deploy")
DEFAULT_SECRET = "slack-webhook"
DEFAULT_DEPLOY_CONNECTOR = "platform-deploy"
TIMEOUT = 15
NOTES_LIMIT = 2500  # a Slack section holds 3000 characters
TICKETS_SHOWN = 20
LOOPBACK = ("localhost", "127.0.0.1", "::1")


def questions(req: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = [
        {
            "id": "webhook_secret",
            "type": "text",
            "prompt": "Name of the secret that holds the Slack webhook URL (set it with `release secret set <name>`)",
            "default": DEFAULT_SECRET,
            "pattern": secrets.NAME_RE.pattern,
            "persist": "project",
        },
        {
            "id": "notify_on",
            "type": "choice",
            "prompt": "Notify Slack on final releases only, or on every deploy (RCs too)?",
            "options": list(NOTIFY_ON),
            "default": "final",
            "persist": "project",
        },
    ]
    send: dict[str, Any] = {"id": "send", "type": "bool", "prompt": f"Send the Slack notice for {req['release_version']}?", "default": True}
    if req.get("mode") == "rc":
        send["when"] = {"notify_on": "every-deploy"}  # an RC under notify_on=final is never announced: no point asking
    items.append(send)
    return {"protocol": 1, "questions": items}


def _fail(error: str, **result: Any) -> dict[str, Any]:
    return {"protocol": 1, "ok": False, "error": error, "result": {"sent": False, **result}}


def _skip(reason: str) -> dict[str, Any]:
    return {"protocol": 1, "ok": True, "result": {"sent": False, "reason": reason}}


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if "\n" in cut:
        cut = cut.rsplit("\n", 1)[0]
    return cut.rstrip() + "…"


def _ok_entry(prior: dict[str, Any], name: str) -> dict[str, Any] | None:
    entry = prior.get(name)
    if isinstance(entry, dict) and entry.get("status") == "ok" and isinstance(entry.get("result"), dict):
        return entry["result"]
    return None


def _jira_text(jira: dict[str, Any]) -> str | None:
    """The released Jira version and the tickets it delivered; RCs write nothing to Jira, so nothing to show."""
    if jira.get("mode") != "fv" or not jira.get("jira_version"):
        return None
    name = _esc(str(jira["jira_version"]))
    version = f"<{jira['url']}|{name}>" if isinstance(jira.get("url"), str) and jira["url"].startswith("https://") else name
    lines = [f"*Jira version* {version}" + (" (released)" if jira.get("released") else "")]
    issues = [i for i in jira.get("issues") or [] if isinstance(i, dict) and i.get("status") in ("delivered", "already")]
    for issue in issues[:TICKETS_SHOWN]:
        lines.append(f"• {_esc(str(issue.get('key', '?')))} {_esc(str(issue.get('summary', '')))}".rstrip())
    if len(issues) > TICKETS_SHOWN:
        lines.append(f"…and {len(issues) - TICKETS_SHOWN} more")
    return _trim("\n".join(lines), 2900)


def build_payload(req: dict[str, Any], deploy: dict[str, Any] | None, notes: str | None) -> dict[str, Any]:
    version = req["release_version"]
    prior = req.get("prior") or {}
    title = f"{req['repo']} {version}"
    fields = [f"*Version*\n{_esc(version)}"]
    if deploy:
        fields.append(f"*Environment*\n{_esc(str(deploy.get('environment') or 'unknown'))}")
        fields.append(f"*Deploy type*\n{_esc(str(deploy.get('deploy_type') or 'unknown'))}")
    gh = _ok_entry(prior, "github-release")
    if gh and isinstance(gh.get("url"), str) and gh["url"].startswith("https://"):
        fields.append(f"*Release*\n<{gh['url']}|GitHub Release>")
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": _trim(title, 150)}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields]},
    ]
    jira = _ok_entry(prior, "jira")
    jira_text = _jira_text(jira) if jira else None
    if jira_text:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": jira_text}})
    if notes and notes.strip():
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _trim(_esc(notes.strip()), NOTES_LIMIT)}})
    summary = f"{title} released" + (f" to {deploy.get('environment')}" if deploy and deploy.get("environment") else "")
    return {"text": summary, "blocks": blocks}


def _webhook_ok(url: str) -> bool:
    parts = urlsplit(url)
    return bool(parts.hostname) and (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK))


def post(url: str, payload: dict[str, Any]) -> str | None:
    """POST the payload; None on success, else a reason that never contains the URL."""
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:  # noqa: S310 (scheme checked by _webhook_ok)
            resp.read()
        return None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace").strip()[:200]
        reason = f"Slack answered HTTP {exc.code}" + (f": {body}" if body else "")
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        reason = f"could not reach Slack: {exc}"
    path = urlsplit(url).path
    reason = reason.replace(url, "<webhook>")
    return reason.replace(path, "<webhook path>") if len(path) > 1 else reason  # http.client quotes the path alone


def run(req: dict[str, Any]) -> dict[str, Any]:
    answers = req.get("answers") or {}
    cfg = req.get("config") or {}
    if not answers.get("send"):
        return _skip("declined" if "send" in answers else f"notify_on is {answers.get('notify_on') or cfg.get('notify_on') or 'final'}: release candidates are not announced")
    notify_on = answers.get("notify_on") or cfg.get("notify_on") or "final"
    if notify_on not in NOTIFY_ON:
        return _fail(f"notify_on must be one of {', '.join(NOTIFY_ON)}, got {notify_on!r}")
    mode = req.get("mode") or ("rc" if "-rc" in req["release_version"] else "fv")
    if notify_on == "final" and mode != "fv":
        return _skip("notify_on is final: release candidates are not announced")
    deploy_name = cfg.get("deploy_connector") or DEFAULT_DEPLOY_CONNECTOR
    entry = (req.get("prior") or {}).get(deploy_name)
    deploy: dict[str, Any] | None = None
    if entry is not None:  # no deploy connector in the chain: notify right after the push
        deploy = (entry or {}).get("result") or {}
        if entry.get("status") != "ok" or deploy.get("status") != "SUCCESS":
            return _skip(f"no successful {deploy_name} in this chain")
    secret_name = answers.get("webhook_secret") or cfg.get("webhook_secret") or DEFAULT_SECRET
    try:
        url = secrets.get(secret_name)
    except secrets.SecretError as exc:
        return _fail(str(exc))
    if not url:
        return _fail(f"secret {secret_name!r} is not set; run `release secret set {secret_name}`")
    if not _webhook_ok(url):
        return _fail(f"secret {secret_name!r} is not an https webhook URL; fix it with `release secret set {secret_name}`")
    try:
        notes: str | None = release_notes(Path(req["repo_root"]), tag_of(req), scheme_of(req)) if req.get("repo_root") else None
    except (NotesError, OSError):
        notes = None  # the message is still useful without the notes
    problem = post(url, build_payload(req, deploy, notes))
    if problem:
        return _fail(problem)
    print(f"Slack notice sent for {req['release_version']}", file=sys.stderr)
    return {"protocol": 1, "ok": True, "result": {"sent": True}}


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
