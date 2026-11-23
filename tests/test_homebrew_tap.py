from __future__ import annotations

import base64
import functools
import hashlib
import http.server
import io
import json
import shutil
import subprocess
import sys
import tarfile
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

from release_cli import go_build, homebrew_tap
from release_cli.connectors import parse_questions, parse_run

FIXTURE = Path(__file__).parent / "fixtures" / "go-app"
VERSION = "1.2.3"
TAG = "v1.2.3"
REMOTE = "git@github.com:acme/hello.git"
TARGETS = [("darwin", "amd64"), ("darwin", "arm64"), ("linux", "amd64"), ("linux", "arm64")]

# A stand-in for `gh` that keeps a tap in a folder: GET returns the stored file, PUT stores it and counts a commit.
FAKE_GH = """#!{python}
import base64, json, os, sys
state = os.environ["FAKE_GH_STATE"]
args = sys.argv[1:]
with open(os.path.join(state, "calls.log"), "a") as log:
    log.write(json.dumps({{"args": args, "token": os.environ.get("GH_TOKEN")}}) + "\\n")
store = os.path.join(state, "formula.json")
path = next(a for a in args if a.startswith("repos/"))
put = "--method" in args
if not put:
    if not os.path.exists(store):
        sys.stderr.write("gh: Not Found (HTTP 404)\\n")
        sys.exit(1)
    print(open(store).read())
    sys.exit(0)
fields = {{a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a and not a.startswith("-")}}
n = len([l for l in open(os.path.join(state, "calls.log")) if '"PUT"' in l])
json.dump({{"sha": "blob%d" % n, "content": fields["content"], "path": path, "message": fields["message"]}}, open(store, "w"))
print("commit%d" % n)
"""


def _manifest(directory: Path) -> dict[str, Any]:
    artifacts = [
        {"name": f"hello_{VERSION}_{o}_{a}.tar.gz", "kind": "archive", "os": o, "arch": a, "sha256": hashlib.sha256(f"{o}{a}".encode()).hexdigest(), "size": 10}
        for o, a in TARGETS
    ]
    artifacts.append({"name": "checksums.txt", "kind": "checksums", "sha256": "0" * 64, "size": 1})
    manifest = {"app": "hello", "version": VERSION, "tag": TAG, "commit": "abc", "dir": str(directory), "artifacts": artifacts}
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def _req(tmp_path: Path, manifest: dict[str, Any], **over: Any) -> dict[str, Any]:
    req = {
        "protocol": 1,
        "action": "run",
        "repo_root": str(tmp_path),
        "remote_url": REMOTE,
        "artifact": "hello",
        "release_version": VERSION,
        "tag": TAG,
        "tag_format": ["v{version}"],
        "mode": "final",
        "dry_run": False,
        "out_dir": None,
        "prior": {"go-build": {"status": "ok", "result": manifest}, "github-release": {"status": "ok", "result": {"published": True}}},
        "config": {"tap": "acme/homebrew-tap", "token_secret": "homebrew-tap-token"},
    }
    return {**req, **over}


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir, state = tmp_path / "ghbin", tmp_path / "ghstate"
    bin_dir.mkdir()
    state.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable))
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    monkeypatch.setenv("RELEASE_SECRET_HOMEBREW_TAP_TOKEN", "tap-token-value")
    return state


def _calls(state: Path) -> list[dict[str, Any]]:
    log = state / "calls.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_dry_run_renders_the_formula_with_every_url_and_checksum(tmp_path: Path, fake_gh: Path) -> None:
    out = tmp_path / "out"  # a dry run shares one folder: go-build's files are already in it
    manifest = _manifest(out)
    resp = homebrew_tap.run(_req(tmp_path, manifest, dry_run=True, out_dir=str(out)))
    outcome = parse_run(resp)
    assert outcome.status == "ok", outcome.error
    formula = (out / "hello.rb").read_text()
    for a in manifest["artifacts"][:4]:
        assert f'url "https://github.com/acme/hello/releases/download/{TAG}/{a["name"]}"' in formula
        assert f'sha256 "{a["sha256"]}"' in formula
    assert 'bin.install "hello"' in formula and "#{bin}/hello version" in formula and formula.startswith("class Hello < Formula")
    assert _calls(fake_gh) == []  # a dry run never reaches the tap
    # the formula is listed next to the other files, whichever connector wrote the manifest
    assert "hello.rb" in [a["name"] for a in json.loads((out / "manifest.json").read_text())["artifacts"]]
    ruby = shutil.which("ruby")
    if ruby:
        check = subprocess.run([ruby, "-c", str(out / "hello.rb")], capture_output=True, text=True)
        assert check.returncode == 0, check.stdout + check.stderr


@pytest.mark.parametrize(
    "release",
    [None, {"status": "ok", "result": {"published": False}}, {"status": "failed", "error": "boom", "result": {"published": False}}],
    ids=["not-in-chain", "declined", "failed"],
)
def test_no_formula_is_committed_when_the_release_assets_are_not_published(tmp_path: Path, fake_gh: Path, release: dict[str, Any] | None) -> None:
    manifest = _manifest(tmp_path / "dist")
    prior = {"go-build": {"status": "ok", "result": manifest}, **({"github-release": release} if release else {})}
    outcome = parse_run(homebrew_tap.run(_req(tmp_path, manifest, prior=prior)))
    assert outcome.status == "ok", outcome.error  # a skip, not a failure
    assert outcome.result["formula"] is None and "github-release" in outcome.result["skipped_reason"]
    assert _calls(fake_gh) == []


def test_a_dry_run_renders_the_formula_without_a_published_release(tmp_path: Path, fake_gh: Path) -> None:
    out = tmp_path / "out"
    manifest = _manifest(out)
    prior = {"go-build": {"status": "ok", "result": manifest}}
    outcome = parse_run(homebrew_tap.run(_req(tmp_path, manifest, prior=prior, dry_run=True, out_dir=str(out))))
    assert outcome.status == "ok", outcome.error
    assert (out / "hello.rb").is_file()


def test_a_release_candidate_is_skipped_with_a_reason(tmp_path: Path, fake_gh: Path) -> None:
    manifest = {**_manifest(tmp_path / "dist"), "version": "1.2.3-rc1"}
    resp = homebrew_tap.run(_req(tmp_path, manifest, release_version="1.2.3-rc1", mode="rc"))
    outcome = parse_run(resp)
    assert outcome.status == "ok"
    assert outcome.result["formula"] is None and "release candidate" in outcome.result["skipped_reason"]
    assert _calls(fake_gh) == []
    assert not (tmp_path / "dist" / "hello.rb").exists()


def test_a_final_commits_the_formula_once_and_a_resume_does_not_commit_again(tmp_path: Path, fake_gh: Path) -> None:
    manifest = _manifest(tmp_path / "dist")
    first = parse_run(homebrew_tap.run(_req(tmp_path, manifest)))
    assert first.status == "ok", first.error
    assert first.result["commit"] == "commit1" and first.result["formula"] == "hello.rb"
    stored = json.loads((fake_gh / "formula.json").read_text())
    assert stored["path"] == "repos/acme/homebrew-tap/contents/Formula/hello.rb"
    assert base64.b64decode(stored["content"]).decode() == (tmp_path / "dist" / "hello.rb").read_text()
    again = parse_run(homebrew_tap.run(_req(tmp_path, manifest)))
    assert again.status == "ok" and "commit" not in again.result and "already" in again.result["skipped_reason"]
    assert len([c for c in _calls(fake_gh) if "--method" in c["args"]]) == 1
    # the token travels in the environment, never on the command line
    assert {c["token"] for c in _calls(fake_gh)} == {"tap-token-value"}
    assert not any("tap-token-value" in " ".join(c["args"]) for c in _calls(fake_gh))


def test_a_new_release_updates_the_formula_it_finds_in_the_tap(tmp_path: Path, fake_gh: Path) -> None:
    assert parse_run(homebrew_tap.run(_req(tmp_path, _manifest(tmp_path / "dist")))).status == "ok"
    newer = {**_manifest(tmp_path / "dist2"), "version": "1.3.0"}
    for a in newer["artifacts"]:
        a["name"] = a["name"].replace(VERSION, "1.3.0")
    out = parse_run(homebrew_tap.run(_req(tmp_path, newer, release_version="1.3.0", tag="v1.3.0")))
    assert out.status == "ok", out.error
    put = [c for c in _calls(fake_gh) if "--method" in c["args"]][-1]
    assert "sha=blob1" in put["args"]  # the update names the file version it replaces, or the API refuses it


def test_missing_prerequisites_fail_with_what_to_do(tmp_path: Path, fake_gh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path / "dist")
    no_tap = parse_run(homebrew_tap.run(_req(tmp_path, manifest, config={})))
    assert no_tap.status == "failed" and "tap" in no_tap.error
    no_build = parse_run(homebrew_tap.run(_req(tmp_path, manifest, prior={})))
    assert no_build.status == "failed" and "go-build" in no_build.error
    monkeypatch.delenv("RELEASE_SECRET_HOMEBREW_TAP_TOKEN")
    monkeypatch.setattr("release_cli.secrets.get", lambda name: None)
    no_secret = parse_run(homebrew_tap.run(_req(tmp_path, manifest)))
    assert no_secret.status == "failed" and "release secret set homebrew-tap-token" in no_secret.error
    assert _calls(fake_gh) == []


def test_questions_declare_dry_run_safe_and_skip_non_github_remotes(tmp_path: Path) -> None:
    qs = parse_questions(homebrew_tap.questions(_req(tmp_path, {})))
    assert qs.applies and qs.dry_run_safe
    assert parse_questions(homebrew_tap.questions(_req(tmp_path, {}, remote_url="https://git.example.org/acme/hello.git"))).applies is False


# --- install.sh: rendered by go-build, run against a local file server ---------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path, dict[str, Any]]]:
    """The fixture app built by go-build (dry run), published under <root>/<tag>/ on a local http server."""
    if shutil.which("go") is None:
        pytest.skip("needs the go toolchain")
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = T\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "app"
    shutil.copytree(FIXTURE, repo)
    _git(repo, "init")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "release")
    out = tmp_path / "out"
    out.mkdir()
    req = {
        "protocol": 1, "action": "run", "repo_root": str(repo), "remote_url": REMOTE, "artifact": "hello", "release_version": "0.1.0",
        "tag": "v0.1.0", "tag_format": ["v{version}"], "mode": "final", "dry_run": True, "out_dir": str(out), "prior": {},
        "sha": _git(repo, "rev-parse", "HEAD"), "config": {"main": "./cmd/hello"},
    }  # fmt: skip
    resp = go_build.run(req)
    assert resp["ok"], resp
    root = tmp_path / "www"
    shutil.copytree(out, root / "v0.1.0")
    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", root / "v0.1.0", resp["result"]
    finally:
        server.shutdown()
        server.server_close()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


def _install(script: Path, base: str, prefix: Path, **env: str) -> subprocess.CompletedProcess[str]:
    run_env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "BASE_URL": base, "PREFIX": str(prefix), **env}
    return subprocess.run(["sh", str(script)], capture_output=True, text=True, env=run_env, timeout=60)


def _host_has_archive(root: Path) -> bool:
    goos = subprocess.run(["go", "env", "GOHOSTOS"], capture_output=True, text=True, check=True).stdout.strip()
    goarch = subprocess.run(["go", "env", "GOHOSTARCH"], capture_output=True, text=True, check=True).stdout.strip()
    return (root / f"hello_0.1.0_{goos}_{goarch}.tar.gz").exists()


def test_install_script_installs_the_host_archive_into_the_prefix(served: tuple[str, Path, dict[str, Any]], tmp_path: Path) -> None:
    base, root, manifest = served
    if not _host_has_archive(root):
        pytest.skip("host is not a default target")
    installer = next(a for a in manifest["artifacts"] if a["kind"] == "installer")
    assert installer["name"] == "install.sh" and (root / "install.sh").stat().st_mode & 0o111
    prefix = tmp_path / "prefix"
    done = _install(root / "install.sh", base, prefix)
    assert done.returncode == 0, done.stdout + done.stderr
    assert subprocess.run([str(prefix / "bin" / "hello"), "version"], capture_output=True, text=True).stdout.strip() == "0.1.0"
    # asking for a version that was never published fails and installs nothing
    other = tmp_path / "other"
    missing = _install(root / "install.sh", base, other, VERSION="9.9.9")
    assert missing.returncode != 0 and not (other / "bin" / "hello").exists()


def test_install_script_refuses_a_tampered_archive(served: tuple[str, Path, dict[str, Any]], tmp_path: Path) -> None:
    base, root, _ = served
    if not _host_has_archive(root):
        pytest.skip("host is not a default target")
    for archive in root.glob("*.tar.gz"):  # whichever archive the host picks: its checksums.txt line no longer matches
        with tarfile.open(archive, "w:gz") as tar:
            data = b"#!/bin/sh\necho pwned\n"
            info = tarfile.TarInfo("hello")
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    prefix = tmp_path / "prefix"
    done = _install(root / "install.sh", base, prefix)
    assert done.returncode != 0 and "checksum mismatch" in done.stderr
    assert not (prefix / "bin" / "hello").exists()


def test_install_template_is_shellcheck_clean() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed")
    template = Path(go_build.__file__).parent / "templates" / "install.sh"
    done = subprocess.run(["shellcheck", str(template)], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout
