from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

from release_cli import npm_publish
from release_cli.connectors import parse_questions, parse_run

FIXTURE = Path(__file__).parent / "fixtures" / "go-app"
PKG_DIR = "packages/hello-js"
TOKEN = "npm_SECRET_TOKEN_123"

# A stand-in for `npm`: `pack` writes a real tarball, `view` answers from $FAKE_NPM_PUBLISHED, every call is logged.
FAKE_NPM = """#!{python}
import json, os, sys, tarfile
args = sys.argv[1:]
log = os.environ["FAKE_NPM_LOG"]
rc = args[args.index("--userconfig") + 1] if "--userconfig" in args else None
entry = {{"args": args, "npmrc": open(rc).read() if rc else None, "secret_env": sorted(k for k in os.environ if k.startswith("RELEASE_SECRET_"))}}
if args[0] == "pack":
    entry["files"] = sorted(os.path.relpath(os.path.join(d, f), args[1]) for d, _, fs in os.walk(args[1]) for f in fs)
    entry["pkg"] = json.load(open(os.path.join(args[1], "package.json")))
open(log, "a").write(json.dumps(entry) + "\\n")
if args[0] == "pack":
    src, dest = args[1], args[args.index("--pack-destination") + 1]
    pkg = json.load(open(os.path.join(src, "package.json")))
    name = pkg["name"].lstrip("@").replace("/", "-") + "-" + pkg["version"] + ".tgz"
    with tarfile.open(os.path.join(dest, name), "w:gz") as tar:
        tar.add(os.path.join(src, "package.json"), "package/package.json")
    print(json.dumps([{{"filename": name}}]))
elif args[0] == "view":
    if args[1] in os.environ.get("FAKE_NPM_PUBLISHED", "").split(","):
        print(args[1].rsplit("@", 1)[1])
    elif os.environ.get("FAKE_NPM_VIEW_BROKEN"):
        print("npm ERR! network trouble, token " + (open(rc).read() if rc else ""), file=sys.stderr)
        sys.exit(1)
    else:
        print("npm ERR! code E404", file=sys.stderr)
        sys.exit(1)
elif args[0] == "publish" and os.environ.get("FAKE_NPM_PUBLISH_FAILS"):
    print("npm ERR! 403 Forbidden, config " + (open(rc).read() if rc else ""), file=sys.stderr)
    sys.exit(1)
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = T\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "app"
    shutil.copytree(FIXTURE, root)
    _git(root, "init")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "release")
    return root


@pytest.fixture
def fake_npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "npm"
    script.write_text(FAKE_NPM.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / "npm.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_NPM_LOG", str(log))
    monkeypatch.delenv("RELEASE_SECRET_NPM_TOKEN", raising=False)
    # no keychain lookups on the developer's machine
    monkeypatch.setattr("release_cli.secrets._has", lambda tool: False)
    return log


def _calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _req(repo: Path, out: Path, version: str = "0.1.0-rc0", **over: Any) -> dict[str, Any]:
    out.mkdir(exist_ok=True)
    manifest = {"app": "hello", "version": version, "dir": str(out), "artifacts": [{"name": "checksums.txt", "kind": "checksums"}]}
    (out / "manifest.json").write_text(json.dumps(manifest))
    req = {
        "protocol": 1,
        "action": "run",
        "repo_root": str(repo),
        "artifact": "hello",
        "release_version": version,
        "sha": _git(repo, "rev-parse", "HEAD"),
        "mode": "rc" if "-" in version else "final",
        "dry_run": False,
        "out_dir": str(out),
        "prior": {"go-build": {"status": "ok", "result": manifest}},
        "config": {"package_dir": PKG_DIR},
    }
    return {**req, **over}


def _tgz_version(path: Path) -> str:
    with tarfile.open(path) as tar:
        member = tar.extractfile("package/package.json")
        assert member is not None
        return json.load(member)["version"]


def test_packs_the_released_commit_not_the_working_tree(repo: Path, tmp_path: Path, fake_npm: Path) -> None:
    pkg = repo / PKG_DIR
    committed = json.loads((pkg / "package.json").read_text())
    (pkg / ".env").write_text("SECRET=1\n")  # untracked
    (pkg / "build").mkdir()
    (pkg / "build" / "stray.js").write_text("x")  # untracked folder
    (pkg / "package.json").write_text(json.dumps({**committed, "description": "uncommitted edit"}))
    outcome = parse_run(npm_publish.run(_req(repo, tmp_path / "out")))
    assert outcome.status == "ok", outcome.error
    pack = next(c for c in _calls(fake_npm) if c["args"][0] == "pack")
    assert pack["files"] == ["package.json"]
    assert pack["pkg"].get("description") == committed.get("description")


@pytest.mark.skipif(shutil.which("npm") is None, reason="needs npm")
@pytest.mark.parametrize(("version", "dist_tag"), [("0.1.0-rc0", "next"), ("0.1.0", "latest")])
def test_dry_run_with_real_npm_packs_the_lockstep_version(repo: Path, tmp_path: Path, version: str, dist_tag: str) -> None:
    tracked = (repo / PKG_DIR / "package.json").read_text()
    out = tmp_path / "out"
    outcome = parse_run(npm_publish.run(_req(repo, out, version, dry_run=True)))
    assert outcome.status == "ok", outcome.error
    assert outcome.result["name"] == "@acme/hello"
    assert (outcome.result["version"], outcome.result["dist_tag"], outcome.result["published"]) == (version, dist_tag, False)
    tgz = out / f"acme-hello-{version}.tgz"
    assert _tgz_version(tgz) == version
    assert outcome.result["artifact"]["name"] == tgz.name
    assert (repo / PKG_DIR / "package.json").read_text() == tracked  # the tracked file keeps its SNAPSHOT version
    assert [a["name"] for a in json.loads((out / "manifest.json").read_text())["artifacts"]] == ["checksums.txt", tgz.name]


def test_real_run_publishes_the_tarball_with_tag_and_access(repo: Path, tmp_path: Path, fake_npm: Path) -> None:
    out = tmp_path / "out"
    outcome = parse_run(npm_publish.run(_req(repo, out, config={"package_dir": PKG_DIR, "access": "restricted"})))
    assert outcome.status == "ok", outcome.error
    assert outcome.result["published"] is True and outcome.result["dist_tag"] == "next"
    view, pack, publish = _calls(fake_npm)
    assert view["args"][:3] == ["view", "@acme/hello@0.1.0-rc0", "version"]
    assert publish["args"][:2] == ["publish", str(out / "acme-hello-0.1.0-rc0.tgz")]
    assert publish["args"][publish["args"].index("--tag") + 1] == "next"
    assert publish["args"][publish["args"].index("--access") + 1] == "restricted"
    assert "--dry-run" not in publish["args"] and "--userconfig" not in publish["args"]  # npm login is used


def test_final_release_publishes_under_latest(repo: Path, tmp_path: Path, fake_npm: Path) -> None:
    assert npm_publish.run(_req(repo, tmp_path / "out", "1.2.3"))["ok"]
    publish = _calls(fake_npm)[-1]["args"]
    assert publish[publish.index("--tag") + 1] == "latest"


def test_dry_run_uses_npm_dry_run_and_never_asks_the_registry(repo: Path, tmp_path: Path, fake_npm: Path) -> None:
    assert npm_publish.run(_req(repo, tmp_path / "out", dry_run=True))["ok"]
    calls = _calls(fake_npm)
    assert [c["args"][0] for c in calls] == ["pack", "publish"]
    assert "--dry-run" in calls[-1]["args"]


def test_version_already_on_the_registry_is_a_resume_safe_success(repo: Path, tmp_path: Path, fake_npm: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_NPM_PUBLISHED", "@acme/hello@0.1.0-rc0")
    outcome = parse_run(npm_publish.run(_req(repo, tmp_path / "out")))
    assert outcome.status == "ok", outcome.error
    assert outcome.result["published"] is False
    assert [c["args"][0] for c in _calls(fake_npm)] == ["view"]  # nothing was packed or uploaded


def test_registry_check_errors_other_than_not_found_fail_instead_of_publishing(repo: Path, tmp_path: Path, fake_npm: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_NPM_VIEW_BROKEN", "1")
    resp = npm_publish.run(_req(repo, tmp_path / "out"))
    assert resp["ok"] is False and "could not check the registry" in resp["error"]
    assert all(c["args"][0] != "publish" for c in _calls(fake_npm))


def test_token_goes_to_a_temp_npmrc_and_never_into_output(repo: Path, tmp_path: Path, fake_npm: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RELEASE_SECRET_NPM_TOKEN", TOKEN)
    monkeypatch.setenv("FAKE_NPM_PUBLISH_FAILS", "1")
    failed = npm_publish.run(_req(repo, tmp_path / "out"))
    assert failed["ok"] is False and "npm publish failed" in failed["error"]
    publish = _calls(fake_npm)[-1]
    assert f"_authToken={TOKEN}" in publish["npmrc"]
    assert not Path(publish["args"][publish["args"].index("--userconfig") + 1]).exists()  # temp file removed
    monkeypatch.delenv("FAKE_NPM_PUBLISH_FAILS")
    ok = npm_publish.run(_req(repo, tmp_path / "out2"))
    seen = json.dumps([failed, ok]) + capsys.readouterr().err
    assert ok["ok"] and TOKEN not in seen


def test_dependency_install_never_sees_the_token_and_runs_no_install_scripts(ts_repo: Path, tmp_path: Path, fake_npm: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a dependency's install script would run inside `npm ci`: it must not be able to read the publish token
    monkeypatch.setenv("RELEASE_SECRET_NPM_TOKEN", TOKEN)
    assert npm_publish.run(_req(ts_repo, tmp_path / "out", config={"package_dir": TS_PKG_DIR}))["ok"]
    ci = next(c for c in _calls(fake_npm) if c["args"][0] == "ci")
    assert "--ignore-scripts" in ci["args"]
    assert "--userconfig" not in ci["args"] and TOKEN not in json.dumps(ci)
    pack = next(c for c in _calls(fake_npm) if c["args"][0] == "pack")  # runs `prepack`: dependency code too
    assert ci["secret_env"] == [] and pack["secret_env"] == []


def test_named_secret_that_is_not_set_fails_with_the_command_to_run(repo: Path, tmp_path: Path, fake_npm: Path) -> None:
    resp = npm_publish.run(_req(repo, tmp_path / "out", config={"package_dir": PKG_DIR, "token_secret": "my-npm"}))
    assert resp["ok"] is False
    assert "release secret set my-npm" in resp["error"]
    assert _calls(fake_npm) == []


def test_needs_a_successful_go_build_in_the_chain(repo: Path, tmp_path: Path, fake_npm: Path) -> None:
    resp = npm_publish.run(_req(repo, tmp_path / "out", prior={}))
    assert resp["ok"] is False and "go-build" in resp["error"]


@pytest.mark.parametrize("config", [{}, {"package_dir": "nope"}, {"package_dir": "../outside"}, {"package_dir": PKG_DIR, "rc_dist_tag": "latest"}])
def test_bad_config_fails_before_running_npm(repo: Path, tmp_path: Path, fake_npm: Path, config: dict[str, Any]) -> None:
    assert npm_publish.run(_req(repo, tmp_path / "out", config=config))["ok"] is False
    assert _calls(fake_npm) == []


def test_questions_declare_dry_run_safe() -> None:
    assert parse_questions(npm_publish.questions({})).dry_run_safe is True


def test_cli_entry_point_speaks_protocol_1(repo: Path, tmp_path: Path, fake_npm: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "release_cli.npm_publish", "run"],
        input=json.dumps(_req(repo, tmp_path / "out", dry_run=True)),
        capture_output=True,
        text=True,
        env={"PATH": f"{fake_npm.parent / 'bin'}:/usr/bin:/bin", "FAKE_NPM_LOG": str(fake_npm), "PYTHONPATH": str(Path(__file__).parent.parent)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True


# --- TypeScript package whose `prepack` builds: needs the whole commit and its dependencies (real npm, network) ---

TS_FIXTURE = Path(__file__).parent / "fixtures" / "ts-workspace"
TS_PKG_DIR = "packages/acme-cli"
needs_npm = pytest.mark.skipif(shutil.which("npm") is None, reason="needs npm")


@pytest.fixture
def ts_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = T\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "ts-app"
    shutil.copytree(TS_FIXTURE, root)
    _git(root, "init")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "release")
    return root


def _tgz_file(path: Path, name: str) -> str:
    with tarfile.open(path) as tar:
        member = tar.extractfile(name)
        assert member is not None
        return member.read().decode()


@needs_npm
def test_workspace_package_is_built_by_prepack_from_the_release_commit(ts_repo: Path, tmp_path: Path) -> None:
    (ts_repo / TS_PKG_DIR / "src" / "index.ts").write_text('export const hello = (): string => "uncommitted";\n')  # not in the commit
    out = tmp_path / "out"
    outcome = parse_run(npm_publish.run(_req(ts_repo, out, dry_run=True, config={"package_dir": TS_PKG_DIR})))
    assert outcome.status == "ok", outcome.error
    tgz = out / "acme-cli-0.1.0-rc0.tgz"
    assert _tgz_version(tgz) == "0.1.0-rc0"
    built = _tgz_file(tgz, "package/dist/index.js")
    assert '"hello"' in built and "uncommitted" not in built


@needs_npm
def test_failing_prepack_fails_the_step_with_npm_output_and_the_folder(repo: Path, tmp_path: Path) -> None:
    pkg = repo / PKG_DIR / "package.json"
    pkg.write_text(json.dumps({**json.loads(pkg.read_text()), "scripts": {"prepack": "echo BUILD_BROKE >&2 && exit 1"}}))
    _git(repo, "commit", "-am", "broken prepack")
    outcome = parse_run(npm_publish.run(_req(repo, tmp_path / "out", dry_run=True)))
    assert outcome.status == "failed"
    assert "BUILD_BROKE" in outcome.error and "npm pack failed in" in outcome.error and PKG_DIR in outcome.error


def test_install_runs_at_the_root_lockfile_else_in_the_package_else_not_at_all(ts_repo: Path, tmp_path: Path, fake_npm: Path) -> None:
    def first_calls(out: str) -> list[str]:
        _git(ts_repo, "add", "-A")
        _git(ts_repo, "commit", "-qm", "x", "--allow-empty")
        assert parse_run(npm_publish.run(_req(ts_repo, tmp_path / out, dry_run=True, config={"package_dir": TS_PKG_DIR}))).status == "ok"
        calls = [c["args"][0] for c in _calls(fake_npm)]
        fake_npm.unlink()
        return calls[:2]

    assert first_calls("a") == ["ci", "pack"]  # root lockfile
    (ts_repo / TS_PKG_DIR / "package-lock.json").write_text("{}")
    (ts_repo / "package-lock.json").unlink()
    assert first_calls("b") == ["ci", "pack"]  # the package's own lockfile
    (ts_repo / TS_PKG_DIR / "package-lock.json").unlink()
    assert first_calls("c")[0] == "pack"  # none
