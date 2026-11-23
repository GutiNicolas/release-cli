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
        f"assets_file = {str(tmp_path / 'assets.json')!r}\n"
        "assets = json.load(open(assets_file)) if os.path.exists(assets_file) else []\n"
        "if args[:2] == ['release', 'view'] and 'assets' in args:\n"
        "    print(json.dumps({'assets': assets})); sys.exit(0)\n"
        "if args[:2] == ['release', 'upload']:\n"
        "    name = os.path.basename(args[3])\n"
        "    if name == os.environ.get('FAKE_GH_FAIL_UPLOAD'):\n"
        "        print('HTTP 502', file=sys.stderr); sys.exit(1)\n"
        "    assets = [a for a in assets if a['name'] != name] + [{'name': name, 'size': os.path.getsize(args[3])}]\n"
        "    json.dump(assets, open(assets_file, 'w')); sys.exit(0)\n"
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


def _manifest_prior(tmp_path: Path, status: str = "ok") -> dict:
    out = tmp_path / "dist"
    out.mkdir(exist_ok=True)
    artifacts = []
    for name, kind in [("app_1.0.0_linux_amd64.tar.gz", "archive"), ("app_1.0.0_darwin_arm64.tar.gz", "archive"), ("checksums.txt", "checksums"), ("install.sh", "installer"), ("app-1.0.0.tgz", "npm")]:
        (out / name).write_bytes(name.encode())
        artifacts.append({"name": name, "kind": kind, "sha256": "x", "size": len(name)})
    result = {"app": "app", "version": "1.0.0", "tag": "1.0.0-rc1", "commit": "abc", "dir": str(out), "artifacts": artifacts}
    return {"go-build": {"status": status, "result": result, "error": None, "job_id": None}}


def _uploads(log: Path) -> list[str]:
    return [Path(c["args"][3]).name for c in _calls(log) if c["args"][:2] == ["release", "upload"]]


def test_run_uploads_every_manifest_asset_but_the_npm_tgz_and_resume_sends_only_missing(repo: Path, fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    req = {**_req(repo, "1.0.0-rc1"), "prior": _manifest_prior(tmp_path)}
    monkeypatch.setenv("FAKE_GH_FAIL_UPLOAD", "install.sh")
    failed = gr.run(req)
    assert failed["ok"] is False and "install.sh" in failed["error"] and "HTTP 502" in failed["error"]
    assert failed["result"]["url"].endswith("1.0.0-rc1")  # the release exists, so a resume reuses it
    calls = _calls(fake_gh)
    assert [c["args"][:2] for c in calls if c["args"][0] == "release"][:2] == [["release", "view"], ["release", "create"]]
    assert all("--clobber" in c["args"] for c in calls if c["args"][:2] == ["release", "upload"])

    monkeypatch.delenv("FAKE_GH_FAIL_UPLOAD")
    fake_gh.unlink()
    resumed = parse_run(gr.run(req))
    assert resumed.status == "ok"
    assert _uploads(fake_gh) == ["install.sh"]  # the archives and checksums made it the first time
    assert not [c for c in _calls(fake_gh) if c["args"][:2] == ["release", "create"]]
    assert resumed.result["assets"] == ["app_1.0.0_linux_amd64.tar.gz", "app_1.0.0_darwin_arm64.tar.gz", "checksums.txt", "install.sh"]
    assert resumed.result["uploaded"] == ["install.sh"]


def test_run_reuploads_an_asset_whose_size_differs(repo: Path, fake_gh: Path, tmp_path: Path) -> None:
    req = {**_req(repo, "1.0.0-rc1"), "prior": _manifest_prior(tmp_path)}
    assert gr.run(req)["ok"]
    assert len(_uploads(fake_gh)) == 4
    assets = json.loads((tmp_path / "assets.json").read_text())
    next(a for a in assets if a["name"] == "checksums.txt")["size"] = 1  # a truncated upload
    (tmp_path / "assets.json").write_text(json.dumps(assets))
    fake_gh.unlink()
    assert gr.run(req)["result"]["uploaded"] == ["checksums.txt"]


def test_run_without_a_good_go_build_uploads_nothing(repo: Path, fake_gh: Path, tmp_path: Path) -> None:
    for prior in ({}, _manifest_prior(tmp_path, status="failed")):
        res = gr.run({**_req(repo, "1.0.0-rc1"), "prior": prior})
        assert res["ok"] and "assets" not in res["result"]
    assert _uploads(fake_gh) == []


def test_run_fails_when_a_listed_asset_is_not_on_disk(repo: Path, fake_gh: Path, tmp_path: Path) -> None:
    prior = _manifest_prior(tmp_path)
    (tmp_path / "dist" / "checksums.txt").unlink()
    res = gr.run({**_req(repo, "1.0.0-rc1"), "prior": prior})
    assert res["ok"] is False and "checksums.txt" in res["error"]
    assert not fake_gh.exists()  # nothing was created before the check


def test_dry_run_lists_the_assets_and_calls_no_gh(repo: Path, fake_gh: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert parse_questions(gr.questions({"release_version": "1.0.0", "remote_url": "git@github.com:o/r.git"})).dry_run_safe
    out = parse_run(gr.run({**_req(repo, "1.0.0-rc1"), "dry_run": True, "prior": _manifest_prior(tmp_path)}))
    assert out.status == "ok" and out.result["published"] is False
    assert out.result["assets"] == ["app_1.0.0_linux_amd64.tar.gz", "app_1.0.0_darwin_arm64.tar.gz", "checksums.txt", "install.sh"]
    assert "install.sh" in capfd.readouterr().err
    assert not fake_gh.exists()
