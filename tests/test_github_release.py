from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from release_cli import github_release as gr
from release_cli.connectors import parse_questions, parse_run


def test_previous_tag_orders_rc_before_final_and_ignores_noise() -> None:
    tags = ["1.1.0", "1.2.0-rc1", "1.2.0-rc2", "app-1.2.0-rc3", "junk", "1.3.0-SNAPSHOT", "1.2.0"]
    assert gr.previous_tag("1.2.0-rc2", tags) == "1.2.0-rc1"
    assert gr.previous_tag("1.2.0-rc1", tags) == "1.1.0"
    assert gr.previous_tag("1.2.0", tags) == "1.1.0"  # finals skip RCs
    assert gr.previous_tag("1.3.0", tags) == "1.2.0"
    assert gr.previous_tag("1.1.0", ["1.1.0", "app-1.0.0"]) is None


def test_previous_tag_compares_numerically() -> None:
    assert gr.previous_tag("1.10.0", ["1.9.0", "1.2.0"]) == "1.9.0"


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = T\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _commit(repo: Path, message: str) -> None:
    (repo / "f.txt").write_text(message)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


@pytest.fixture
def repo(tmp_path: Path, git_env: None) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _commit(root, "first")
    _commit(root, "[manual-release] 1.0.0-rc0")
    _git(root, "tag", "-a", "1.0.0-rc0", "-m", "rc")
    _git(root, "tag", "-a", "app-1.0.0-rc0", "-m", "rc")
    _commit(root, "add login")
    _commit(root, "[manual-release] 1.0.0-rc1")
    _commit(root, "fix logout")
    _git(root, "tag", "-a", "1.0.0-rc1", "-m", "rc")
    return root


def test_release_notes_since_previous_tag_drops_tool_commits(repo: Path) -> None:
    notes = gr.release_notes(repo, "1.0.0-rc1")
    lines = notes.splitlines()
    assert lines[0] == "## Changes since 1.0.0-rc0"
    bullets = [ln for ln in lines if ln.startswith("- ")]
    assert [b.rsplit(" (", 1)[0] for b in bullets] == ["- fix logout", "- add login"]
    assert "manual-release" not in notes


def test_release_notes_without_previous_tag_and_when_empty(repo: Path) -> None:
    first = gr.release_notes(repo, "1.0.0-rc0")
    assert first.startswith("## Changes\n")
    assert "- first (" in first
    _git(repo, "tag", "-a", "1.0.0", "-m", "final")
    _commit(repo, "[manual-release] 1.0.1-SNAPSHOT")
    _git(repo, "tag", "-a", "1.0.1", "-m", "final")
    assert "- No changes." in gr.release_notes(repo, "1.0.1")


def test_release_notes_unknown_tag_raises(repo: Path) -> None:
    with pytest.raises(gr.NotesError):
        gr.release_notes(repo, "9.9.9")


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "gh.log"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "gh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"log = {str(log)!r}\n"
        "args = sys.argv[1:]\n"
        "entry = {'args': args, 'cwd': os.getcwd()}\n"
        "if '--notes-file' in args:\n"
        "    entry['notes'] = open(args[args.index('--notes-file') + 1]).read()\n"
        "open(log, 'a').write(json.dumps(entry) + '\\n')\n"
        "if args[:2] == ['release', 'view']:\n"
        f"    if os.path.exists({str(tmp_path / 'created')!r}):\n"
        "        print('https://github.com/o/r/releases/tag/' + args[2]); sys.exit(0)\n"
        "    print('release not found', file=sys.stderr); sys.exit(1)\n"
        "if args[:2] == ['release', 'create']:\n"
        f"    open({str(tmp_path / 'created')!r}, 'w').write('1')\n"
        "    print('https://github.com/o/r/releases/tag/' + args[2]); sys.exit(0)\n"
        "sys.exit(3)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def _req(repo: Path, version: str) -> dict:
    return {"protocol": 1, "action": "run", "repo_root": str(repo), "release_version": version, "answers": {"publish": True}}


def _calls(log: Path) -> list[dict]:
    return [json.loads(ln) for ln in log.read_text().splitlines()]


def test_run_creates_prerelease_with_notes_and_is_idempotent(repo: Path, fake_gh: Path) -> None:
    out = parse_run(gr.run(_req(repo, "1.0.0-rc1")))
    assert out.status == "ok"
    assert out.result == {"published": True, "url": "https://github.com/o/r/releases/tag/1.0.0-rc1", "prerelease": True}
    create = [c for c in _calls(fake_gh) if c["args"][:2] == ["release", "create"]][0]
    assert "--prerelease" in create["args"] and "--verify-tag" in create["args"]
    assert create["args"][2] == "1.0.0-rc1"
    assert Path(create["cwd"]).resolve() == repo.resolve()
    assert "- fix logout (" in create["notes"] and "manual-release" not in create["notes"]

    again = gr.run(_req(repo, "1.0.0-rc1"))
    assert again["ok"] and again["result"]["existed"] is True
    assert len([c for c in _calls(fake_gh) if c["args"][:2] == ["release", "create"]]) == 1


def test_run_final_is_not_prerelease_and_declined_does_nothing(repo: Path, fake_gh: Path) -> None:
    _git(repo, "tag", "-a", "1.0.0", "-m", "final")
    assert gr.run({**_req(repo, "1.0.0"), "answers": {"publish": False}}) == {"protocol": 1, "ok": True, "result": {"published": False}}
    assert not fake_gh.exists()
    res = gr.run(_req(repo, "1.0.0"))
    assert res["result"]["prerelease"] is False
    create = [c for c in _calls(fake_gh) if c["args"][:2] == ["release", "create"]][0]
    assert "--prerelease" not in create["args"]


def test_run_reports_gh_failure_and_missing_gh(repo: Path, fake_gh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res = gr.run(_req(repo, "9.9.9"))  # tag does not exist: notes fail before gh create
    assert res["ok"] is False and "release notes failed" in res["error"]
    monkeypatch.setenv("PATH", "/nonexistent")
    res = gr.run(_req(repo, "1.0.0-rc1"))
    assert res["ok"] is False and "gh auth login" in res["error"]


def test_questions_apply_only_to_github_remotes() -> None:
    base = {"release_version": "1.0.0"}
    for url in ("https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@github.com/o/r"):
        q = parse_questions(gr.questions({**base, "remote_url": url}))
        assert q.applies and [i.id for i in q.items] == ["publish"]
    for url in ("git@gitlab.com:o/r.git", "https://github.com.evil.io/o/r", ""):
        assert parse_questions(gr.questions({**base, "remote_url": url})).applies is False
