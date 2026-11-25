"""`sign` connector: cosign signature of checksums.txt and signed SLSA provenance, checked the way a user would.

The verification uses the real `cosign` CLI (skipped with a reason when it or `go` is missing; CI installs both).
Keys are generated per test, so no key material lives in the repo. Everything runs without network: the signing
config handed to cosign names no transparency log, so nothing is uploaded.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from release_cli import go_build, packaging, secrets, sign
from release_cli.connectors import parse_run

FIXTURE = Path(__file__).parent / "fixtures" / "go-app"
VERSION = "0.1.0-rc0"
TAG = "v0.1.0-rc0"
PASSWORD = "pw-5f3a9c-not-for-logs"
needs_cosign = pytest.mark.skipif(shutil.which("cosign") is None or shutil.which("go") is None, reason="needs the cosign and go toolchains")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _cosign(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["cosign", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, "COSIGN_PASSWORD": PASSWORD}, check=False)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = T\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "app"
    shutil.copytree(FIXTURE, root)
    _git(root, "init")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "release")
    _git(root, "tag", "-a", TAG, "-m", "rc")
    return root


def _req(repo: Path, **over: Any) -> dict[str, Any]:
    req = {
        "protocol": 1, "action": "run", "repo_root": str(repo), "artifact": "hello", "release_version": VERSION,
        "tag": TAG, "tags": [TAG], "tag_format": ["v{version}"], "mode": "rc", "dry_run": False, "out_dir": None,
        "prior": {}, "config": {}, "remote_url": "https://github.com/acme/hello.git", "sha": _git(repo, "rev-parse", "HEAD"),
    }
    return {**req, **over}


def _build(repo: Path, **over: Any) -> dict[str, Any]:
    """Run go-build and return a request for `sign` with its manifest in `prior`."""
    req = _req(repo, config={"main": "./cmd/hello"}, **over)
    resp = go_build.run(req)
    assert resp["ok"], resp
    return _req(repo, **{**over, "prior": {"go-build": {"status": "ok", "result": resp["result"], "error": None, "job_id": None}}})


def _archives(out: Path) -> list[Path]:
    return sorted(out.glob("hello_*.tar.gz"))


@pytest.fixture
def signed(repo: Path, tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """A dry run of go-build + sign: the output folder and the sign response."""
    out = tmp_path / "out"
    out.mkdir()
    resp = sign.run(_build(repo, dry_run=True, out_dir=str(out)))
    assert resp["ok"], resp
    return out, resp


@needs_cosign
def test_dry_run_signs_with_a_throwaway_key_and_every_archive_verifies(signed: tuple[Path, dict[str, Any]], tmp_path: Path) -> None:
    out, resp = signed
    outcome = parse_run(resp)
    assert outcome.status == "ok"
    assert outcome.result["public_key"] == "cosign.pub" and outcome.result["tlog"] is False
    assert outcome.result["signed"] == ["checksums.txt", *(a.name for a in _archives(out))]
    files = ["checksums.txt.sig", f"hello_{VERSION}.intoto.jsonl", "cosign.pub"]
    assert [a["name"] for a in outcome.result["artifacts"]] == files
    assert {a["name"] for a in json.loads((out / "manifest.json").read_text())["artifacts"]} >= set(files)

    verify = ["--key", "cosign.pub", "--insecure-ignore-tlog"]
    checked = _cosign("verify-blob", *verify, "--bundle", "checksums.txt.sig", "checksums.txt", cwd=out)
    assert checked.returncode == 0, checked.stderr
    assert len(_archives(out)) == 4
    for archive in _archives(out):
        att = _cosign("verify-blob-attestation", *verify, "--bundle", files[1], "--type", "slsaprovenance1", archive.name, cwd=out)
        assert att.returncode == 0, att.stderr

    # a tampered archive or checksums file is rejected
    victim = _archives(out)[0]
    victim.write_bytes(victim.read_bytes() + b"x")
    assert _cosign("verify-blob-attestation", *verify, "--bundle", files[1], "--type", "slsaprovenance1", victim.name, cwd=out).returncode != 0
    (out / "checksums.txt").write_text("0" * 64 + "  " + victim.name + "\n")
    assert _cosign("verify-blob", *verify, "--bundle", "checksums.txt.sig", "checksums.txt", cwd=out).returncode != 0


@needs_cosign
def test_provenance_names_the_tag_commit_and_every_archive_digest(signed: tuple[Path, dict[str, Any]], repo: Path) -> None:
    out, _ = signed
    envelope = json.loads((out / f"hello_{VERSION}.intoto.jsonl").read_text())["dsseEnvelope"]
    stmt = json.loads(base64.b64decode(envelope["payload"]))
    manifest = json.loads((out / "manifest.json").read_text())
    expected = {a["name"]: a["sha256"] for a in manifest["artifacts"] if a["kind"] == "archive"}
    assert {s["name"]: s["digest"]["sha256"] for s in stmt["subject"]} == expected and len(expected) == 4
    commit = _git(repo, "rev-parse", "HEAD")
    source = stmt["predicate"]["buildDefinition"]["externalParameters"]["source"]
    assert source == {"repository": "https://github.com/acme/hello", "tag": TAG, "commit": commit}
    assert stmt["predicate"]["buildDefinition"]["externalParameters"]["build"]["main"] == "./cmd/hello"
    assert stmt["predicate"]["runDetails"]["builder"]["version"]["release-cli"]
    assert "tlogEntries" not in json.dumps(json.loads((out / "checksums.txt.sig").read_text())["verificationMaterial"])


def test_credentials_in_the_remote_url_never_reach_the_statement() -> None:
    manifest = {"tag": TAG, "commit": "abc", "artifacts": [{"name": "a.tar.gz", "kind": "archive", "sha256": "f" * 64}]}
    stmt = sign.statement(manifest, {"remote_url": "https://user:hunter2@github.com/acme/hello.git", "tag": TAG})
    assert "hunter2" not in json.dumps(stmt) and stmt["predicate"]["buildDefinition"]["externalParameters"]["source"]["repository"] == "https://github.com/acme/hello"


_ONE_ARCHIVE = {"tag": TAG, "commit": "abc", "artifacts": [{"name": "a.tar.gz", "kind": "archive", "sha256": "f" * 64}]}


@pytest.mark.parametrize("remote", [
    "https://github.com/acme/hello.git",
    "git@github.com:acme/hello.git",
    "ssh://git@github.com/acme/hello.git",
    "ssh://git@github.com:22/acme/hello",
])
def test_every_remote_form_names_the_same_https_source(remote: str) -> None:
    stmt = sign.statement(_ONE_ARCHIVE, {"remote_url": remote, "tag": TAG})
    build = stmt["predicate"]["buildDefinition"]
    assert build["externalParameters"]["source"]["repository"] == "https://github.com/acme/hello"
    assert build["resolvedDependencies"][0]["uri"] == f"git+https://github.com/acme/hello@refs/tags/{TAG}"


@pytest.mark.parametrize("remote", [None, "", "  ", "hello"])
def test_a_remote_that_names_no_repository_is_refused_not_signed_empty(remote: Any) -> None:
    with pytest.raises(sign.SignError, match="remote"):
        sign.statement(_ONE_ARCHIVE, {"remote_url": remote, "tag": TAG})


@needs_cosign
@pytest.mark.parametrize("tamper", ["archive", "checksums-line", "checksums-missing-line"])
def test_files_changed_after_go_build_are_refused_and_nothing_is_signed(tamper: str, repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    req = _build(repo, dry_run=True, out_dir=str(out))
    victim = _archives(out)[0]
    lines = (out / "checksums.txt").read_text().splitlines(keepends=True)
    if tamper == "archive":
        victim.write_bytes(victim.read_bytes() + b"x")
    elif tamper == "checksums-line":
        (out / "checksums.txt").write_text("".join(("0" * 64 + "  " + victim.name + "\n") if victim.name in ln else ln for ln in lines))
    else:
        (out / "checksums.txt").write_text("".join(ln for ln in lines if victim.name not in ln))
    resp = sign.run(req)
    assert resp["ok"] is False and (victim.name in resp["error"] or "checksums.txt" in resp["error"]), resp
    assert not any((out / n).exists() for n in ("checksums.txt.sig", f"hello_{VERSION}.intoto.jsonl", "cosign.pub"))
    assert not list(Path(os.environ["TMPDIR"]).glob("sign-*"))


def _keypair(tmp_path: Path) -> tuple[str, str]:
    keys = tmp_path / "keys"
    keys.mkdir()
    done = _cosign("generate-key-pair", cwd=keys)
    assert done.returncode == 0, done.stderr
    return (keys / "cosign.key").read_text(), (keys / "cosign.pub").read_text()


@needs_cosign
@pytest.mark.parametrize("form", ["pem", "base64"])
def test_real_run_signs_with_the_stored_key_and_never_prints_it(form: str, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    pem, pub = _keypair(tmp_path)
    (repo / "cosign.pub").write_text(pub)
    monkeypatch.setenv("RELEASE_SECRET_COSIGN_KEY", pem if form == "pem" else base64.b64encode(pem.encode()).decode())
    monkeypatch.setenv("RELEASE_SECRET_COSIGN_PASSWORD", PASSWORD)
    req = _build(repo)
    req["config"] = {"public_key": "cosign.pub"}
    resp = sign.run(req)
    assert resp["ok"], resp
    dist = repo / "dist" / VERSION
    assert (dist / "cosign.pub").read_text() == pub
    checked = _cosign("verify-blob", "--key", str(repo / "cosign.pub"), "--insecure-ignore-tlog", "--bundle", "checksums.txt.sig", "checksums.txt", cwd=dist)
    assert checked.returncode == 0, checked.stderr
    # the key and password are in no output, no response (it becomes `prior` and the run file) and no output file
    seen = capfd.readouterr()
    haystack = seen.out + seen.err + json.dumps(resp) + "".join(p.read_text(errors="ignore") for p in dist.iterdir() if p.suffix in {".json", ".jsonl", ".sig", ".txt"})
    for line in [PASSWORD, *pem.strip().splitlines()[1:-1]]:
        assert line not in haystack
    # and no private key file is left behind
    assert not list(Path(os.environ["TMPDIR"]).glob("sign-*"))


@needs_cosign
def test_wrong_password_and_mismatched_public_key_fail_without_leaking(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pem, pub = _keypair(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    assert _cosign("generate-key-pair", cwd=other).returncode == 0
    monkeypatch.setenv("RELEASE_SECRET_COSIGN_KEY", pem)
    monkeypatch.setenv("RELEASE_SECRET_COSIGN_PASSWORD", "not-the-password")
    (repo / "cosign.pub").write_text(pub)
    req = {**_build(repo), "config": {}}
    bad_password = sign.run(req)
    assert bad_password["ok"] is False and "sign-blob" in bad_password["error"] and "not-the-password" not in bad_password["error"]
    monkeypatch.setenv("RELEASE_SECRET_COSIGN_PASSWORD", PASSWORD)
    (repo / "cosign.pub").write_text((other / "cosign.pub").read_text())
    before = (repo / "dist" / VERSION / "manifest.json").read_text()
    wrong_pub = sign.run(req)
    assert wrong_pub["ok"] is False and "verify-blob" in wrong_pub["error"]
    assert (repo / "dist" / VERSION / "manifest.json").read_text() == before  # nothing is advertised as signed


def _prior(tmp_path: Path) -> dict[str, Any]:
    out = tmp_path / "dist"
    out.mkdir(exist_ok=True)
    (out / "checksums.txt").write_text("x\n")
    manifest = {"app": "app", "version": "1.0.0", "tag": "v1.0.0", "commit": "abc", "dir": str(out), "artifacts": [{"name": "app_1.0.0_linux_amd64.tar.gz", "kind": "archive", "sha256": "f" * 64}]}
    return {"go-build": {"status": "ok", "result": manifest, "error": None, "job_id": None}}


def test_missing_cosign_fails_with_the_install_hint(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    req = _req(repo, prior=_prior(tmp_path))
    monkeypatch.setenv("PATH", str(empty))
    resp = sign.run(req)
    assert resp["ok"] is False and "brew install cosign" in resp["error"] and "docs.sigstore.dev" in resp["error"]


def test_missing_secrets_fail_with_the_release_secret_command(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "cosign").write_text("#!/bin/sh\nexit 1\n")
    (fake / "cosign").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(secrets, "_has", lambda _tool: False)  # no keychain: only the environment is consulted
    monkeypatch.delenv("RELEASE_SECRET_COSIGN_KEY", raising=False)
    monkeypatch.delenv("RELEASE_SECRET_COSIGN_PASSWORD", raising=False)
    (repo / "cosign.pub").write_text("pub\n")
    req = _req(repo, prior=_prior(tmp_path))
    no_key = sign.run(req)
    assert no_key["ok"] is False and "release secret set cosign-key" in no_key["error"]
    monkeypatch.setenv("RELEASE_SECRET_COSIGN_KEY", "-----BEGIN ENCRYPTED SECRET-----\nabc\n-----END ENCRYPTED SECRET-----")
    no_password = sign.run(req)
    assert no_password["ok"] is False and "release secret set cosign-password" in no_password["error"]
    (repo / "cosign.pub").unlink()
    no_pub = sign.run(req)
    assert no_pub["ok"] is False and "cosign.pub" in no_pub["error"] and "generate-key-pair" in no_pub["error"]


def test_a_secret_that_is_not_a_private_key_is_refused_without_echoing_it() -> None:
    with pytest.raises(sign.SignError) as err:
        sign.key_pem("hunter2-not-a-key")
    assert "hunter2" not in str(err.value)


def test_sign_needs_a_successful_go_build_and_refuses_a_transparency_log(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "cosign").write_text("#!/bin/sh\nexit 1\n")
    (fake / "cosign").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    no_build = sign.run(_req(repo))
    assert no_build["ok"] is False and "go-build" in no_build["error"]
    tlog = sign.run(_req(repo, prior=_prior(tmp_path), config={"tlog": True}))
    assert tlog["ok"] is False and "transparency log" in tlog["error"]


def _manifest_of(*names: str) -> dict[str, Any]:
    return {"tag": TAG, "commit": "c0ffee", "build": {"main": "./cmd/hello"}, "artifacts": [{"name": n, "kind": "archive", "sha256": "ab" * 32} for n in names]}


def test_without_builder_id_the_statement_is_the_one_signed_before_the_setting_existed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sign, "_release_cli_version", lambda: "9.9.9")
    stmt = sign.statement(_manifest_of("hello_1.tar.gz"), {"remote_url": "https://github.com/acme/hello.git"})
    assert stmt == {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "hello_1.tar.gz", "digest": {"sha256": "ab" * 32}}],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://github.com/GutiNicolas/release-cli/go-build/v1",
                "externalParameters": {"source": {"repository": "https://github.com/acme/hello", "tag": TAG, "commit": "c0ffee"}, "build": {"main": "./cmd/hello"}},
                "resolvedDependencies": [{"uri": f"git+https://github.com/acme/hello@refs/tags/{TAG}", "digest": {"gitCommit": "c0ffee"}}],
            },
            "runDetails": {"builder": {"id": "https://github.com/GutiNicolas/release-cli", "version": {"release-cli": "9.9.9"}}},
        },
    }


def test_builder_id_replaces_the_builder_and_the_tool_name_appears_nowhere() -> None:
    stmt = sign.statement(_manifest_of("hello_1.tar.gz"), {"remote_url": "https://github.com/acme/hello"}, "https://github.com/acme/app")
    assert stmt["predicate"]["runDetails"] == {"builder": {"id": "https://github.com/acme/app"}}
    assert stmt["predicate"]["buildDefinition"]["buildType"] == "https://github.com/acme/app/go-build/v1"
    assert not re.search("release-cli|gutinicolas", json.dumps(stmt), re.IGNORECASE)


@pytest.mark.parametrize("value", ["http://x", "acme", "https://", "", 7, "https://a b/c"])
def test_builder_id_must_be_an_https_url_and_the_error_names_the_key(value: Any) -> None:
    with pytest.raises(sign.SignError, match=r"`builder_id`.*https://"):
        sign._settings({"config": {"builder_id": value}})


def test_builder_id_loses_a_trailing_slash_so_build_type_has_no_double_slash() -> None:
    assert sign._settings({"config": {"builder_id": "https://github.com/acme/app/"}})["builder_id"] == "https://github.com/acme/app"
    assert "builder_id" not in sign._settings({"config": {}})


@needs_cosign
def test_dry_run_with_builder_id_signs_a_neutral_statement_that_still_verifies(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    req = _build(repo, dry_run=True, out_dir=str(out))
    resp = sign.run({**req, "config": {"builder_id": "https://github.com/acme/app"}})
    assert resp["ok"], resp
    attestation = out / f"hello_{VERSION}.intoto.jsonl"
    payload = base64.b64decode(json.loads(attestation.read_text())["dsseEnvelope"]["payload"]).decode()
    assert not re.search(r"release-cli|gutinicolas", payload, re.IGNORECASE)
    stmt = json.loads(payload)
    assert stmt["predicate"]["runDetails"]["builder"] == {"id": "https://github.com/acme/app"}
    assert stmt["predicate"]["buildDefinition"]["buildType"] == "https://github.com/acme/app/go-build/v1"
    verify = ["--key", "cosign.pub", "--insecure-ignore-tlog"]
    assert _cosign("verify-blob", *verify, "--bundle", "checksums.txt.sig", "checksums.txt", cwd=out).returncode == 0
    for archive in _archives(out):
        att = _cosign("verify-blob-attestation", *verify, "--bundle", attestation.name, "--type", "slsaprovenance1", archive.name, cwd=out)
        assert att.returncode == 0, att.stderr


def _windows_release(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """A built Windows release (a .zip and the bare .exe, as cargo-build lays it out) and a dry-run sign request for it."""
    stage, out = tmp_path / "stage", tmp_path / "out"
    stage.mkdir()
    exe = stage / f"hello_{VERSION}_windows_amd64.exe"
    exe.write_bytes(b"MZ-not-a-real-exe")
    packaging.write_zip(stage / f"hello_{VERSION}_windows_amd64.zip", [("hello.exe", exe.read_bytes(), 0o755)])
    artifacts = [packaging.entry(stage / exe.name.replace(".exe", ".zip"), "archive", "windows", "amd64"), packaging.entry(exe, "binary", "windows", "amd64")]
    manifest = packaging.finish(stage, out, artifacts, app="hello", version=VERSION, tag=TAG, sha="c" * 40, build={"profile": "release"})
    req = {
        "protocol": 1, "action": "run", "repo_root": str(tmp_path), "artifact": "hello", "release_version": VERSION, "tag": TAG, "tags": [TAG],
        "tag_format": ["v{version}"], "mode": "rc", "dry_run": True, "out_dir": str(out), "config": {}, "remote_url": "https://github.com/acme/hello.git",
        "prior": {"go-build": {"status": "ok", "result": manifest, "error": None, "job_id": None}},
    }
    return out, req


@needs_cosign
def test_a_bare_binary_is_signed_and_is_a_provenance_subject(tmp_path: Path) -> None:
    out, req = _windows_release(tmp_path)
    resp = sign.run(req)
    assert resp["ok"], resp
    exe = f"hello_{VERSION}_windows_amd64.exe"
    assert resp["result"]["signed"] == ["checksums.txt", exe, f"hello_{VERSION}_windows_amd64.zip"]
    envelope = json.loads((out / f"hello_{VERSION}.intoto.jsonl").read_text())["dsseEnvelope"]
    subjects = {s["name"]: s["digest"]["sha256"] for s in json.loads(base64.b64decode(envelope["payload"]))["subject"]}
    assert subjects[exe] == hashlib.sha256((out / exe).read_bytes()).hexdigest() and len(subjects) == 2
    verify = ["--key", "cosign.pub", "--insecure-ignore-tlog", "--bundle", f"hello_{VERSION}.intoto.jsonl", "--type", "slsaprovenance1"]
    assert _cosign("verify-blob-attestation", *verify, exe, cwd=out).returncode == 0  # the exe a user downloads checks against it
    (out / exe).write_bytes(b"swapped")
    assert _cosign("verify-blob-attestation", *verify, exe, cwd=out).returncode != 0


@needs_cosign
@pytest.mark.parametrize("tamper", ["changed", "deleted", "not-in-checksums"])
def test_a_binary_that_changed_or_is_unlisted_fails_closed(tamper: str, tmp_path: Path) -> None:
    out, req = _windows_release(tmp_path)
    exe = out / f"hello_{VERSION}_windows_amd64.exe"
    if tamper == "changed":
        exe.write_bytes(exe.read_bytes() + b"x")
    elif tamper == "deleted":
        exe.unlink()
    else:
        lines = (out / "checksums.txt").read_text().splitlines(keepends=True)
        (out / "checksums.txt").write_text("".join(ln for ln in lines if exe.name not in ln))
    resp = sign.run(req)
    assert resp["ok"] is False and (exe.name in resp["error"] or "checksums.txt" in resp["error"]), resp
    assert not any((out / n).exists() for n in ("checksums.txt.sig", f"hello_{VERSION}.intoto.jsonl", "cosign.pub"))
