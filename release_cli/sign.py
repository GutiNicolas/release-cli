"""Built-in connector `sign`: sign a Go release with a maintainer key and attach signed SLSA provenance.

Runs after `go-build` (it reads its manifest from `prior`). With `cosign`:
  * signs `checksums.txt` -> `checksums.txt.sig` (a cosign bundle),
  * writes an in-toto statement with a SLSA provenance predicate (subjects: every archive and its sha256; builder:
    release-cli and its version; source: repo, tag, commit; the build flags) and signs it with `cosign attest-blob`
    -> `<binary>_<version>.intoto.jsonl`,
  * copies the public key next to them as `cosign.pub`,
and adds the three to `manifest.json` (and to this connector's result, which `github-release` uploads).
Nothing is sent to a transparency log, so nothing about the maintainer is published.

The private key (PEM) is the secret `cosign-key`, its password `cosign-password` (names can be changed with
`key_secret` / `password_secret`). The key is written to a 0600 file only while cosign runs. A dry run signs with a
throwaway key generated in a temp dir, so the whole chain is checked without the real key.

    python -m release_cli.sign questions|run   (protocol 1 on stdin/stdout)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from release_cli import secrets
from release_cli.tagformat import tag_of

DEFAULT_KEY_SECRET = "cosign-key"
DEFAULT_PASSWORD_SECRET = "cosign-password"
DEFAULT_PUBLIC_KEY = "cosign.pub"
SIGNATURE = "checksums.txt.sig"
PUBLIC_KEY_NAME = "cosign.pub"
REPO_URL = "https://github.com/GutiNicolas/release-cli"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
BUILD_TYPE = f"{REPO_URL}/go-build/v1"
COSIGN_MISSING = "`cosign` is not on PATH; install it (macOS: `brew install cosign`; other systems: https://docs.sigstore.dev/cosign/system_config/installation/) and make sure `cosign version` works in this shell"
# cosign 3 sends every signature to the public Rekor log unless it is given a signing config that names no
# transparency log (or timestamp, certificate or OIDC) service: this one names none.
SIGNING_CONFIG = {"mediaType": "application/vnd.dev.sigstore.signingconfig.v0.2+json", "rekorTlogConfig": {}, "tsaConfig": {}}
ERROR_TAIL = 8
TIMEOUT = 120


class SignError(RuntimeError):
    pass


def questions(req: dict[str, Any]) -> dict[str, Any]:
    if not (Path(req["repo_root"]) / "go.mod").is_file():
        return {"protocol": 1, "applies": False, "reason": "no go.mod at the repo root", "questions": []}
    return {"protocol": 1, "dry_run": "safe", "questions": []}


def _settings(req: dict[str, Any]) -> dict[str, Any]:
    cfg = req.get("config") or {}
    out = {
        "public_key": cfg.get("public_key", DEFAULT_PUBLIC_KEY),
        "key_secret": cfg.get("key_secret", DEFAULT_KEY_SECRET),
        "password_secret": cfg.get("password_secret", DEFAULT_PASSWORD_SECRET),
    }
    for name, value in out.items():
        if not isinstance(value, str) or not value:
            raise SignError(f"`{name}` under [connectors.sign] must be text")
    if cfg.get("tlog", False) is not False:
        raise SignError("`tlog = true` is not supported: `sign` never uploads to a transparency log (leave `tlog` unset or false)")
    return out


def _manifest(req: dict[str, Any]) -> dict[str, Any]:
    entry = (req.get("prior") or {}).get("go-build")
    if not isinstance(entry, dict):
        raise SignError("go-build has not run before this connector; put go-build before sign in the chain (`release connector ls`)")
    result = entry.get("result")
    if entry.get("status") != "ok" or not isinstance(result, dict) or not isinstance(result.get("artifacts"), list):
        raise SignError("go-build did not succeed, so there is nothing to sign")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail(text: str, hide: list[str]) -> str:
    for secret in hide:
        if secret:
            text = text.replace(secret, "***")
    return "\n".join(text.strip().splitlines()[-ERROR_TAIL:])


def key_pem(value: str) -> str:
    """The PEM text of a stored key. `release secret set` reads one line, so a key is stored either as its PEM (a
    keychain item or environment variable may hold real newlines, or the two characters `\\n`) or as the base64 of
    the file (`base64 < cosign.key | tr -d '\\n'`)."""
    text = value.strip()
    if not text.startswith("-----BEGIN"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            text = ""
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    if not text.startswith("-----BEGIN") or "PRIVATE KEY-----" not in text:
        raise SignError("the cosign key secret is not a cosign private key: store the content of cosign.key (or its base64, on one line)")
    return text + "\n"


class Cosign:
    """Runs cosign with the key in a 0600 file inside a private temp dir and the password in the environment only."""

    def __init__(self, exe: str, workdir: Path, key: Path, password: str, hide: list[str]) -> None:
        self.exe, self.workdir, self.key, self.hide = exe, workdir, key, hide
        self.env = {**os.environ, "COSIGN_PASSWORD": password}

    def run(self, *args: str, cwd: Path | None = None) -> None:
        try:
            proc = subprocess.run(
                [self.exe, *args], cwd=cwd or self.workdir, env=self.env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SignError(f"cosign {args[0]} timed out after {TIMEOUT}s") from exc
        if proc.returncode != 0:
            detail = _tail(proc.stderr or proc.stdout, self.hide) or f"exit {proc.returncode}"
            raise SignError(f"cosign {args[0]} failed: {detail}")


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _source_repository(remote: Any) -> str:
    """The remote URL without credentials."""
    if not isinstance(remote, str):
        return ""
    url = remote.strip()
    if "://" in url:
        parts = urlsplit(url)
        host = parts.hostname or ""
        url = f"{parts.scheme}://{host}{f':{parts.port}' if parts.port else ''}{parts.path}"
    return url.removesuffix(".git")


def _release_cli_version() -> str:
    try:
        return metadata.version("release-cli")
    except metadata.PackageNotFoundError:
        return "unknown"


def statement(manifest: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """The in-toto statement: SLSA provenance for every archive of the manifest."""
    archives = sorted((a for a in manifest["artifacts"] if isinstance(a, dict) and a.get("kind") == "archive"), key=lambda a: a["name"])
    if not archives:
        raise SignError("the go-build manifest lists no archive to attest")
    tag, sha = manifest.get("tag") or tag_of(req), manifest["commit"]
    repo = _source_repository(req.get("remote_url"))
    return {
        "_type": STATEMENT_TYPE,
        "subject": [{"name": a["name"], "digest": {"sha256": a["sha256"]}} for a in archives],
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": {
                    "source": {"repository": repo, "tag": tag, "commit": sha},
                    "build": manifest.get("build") or {},
                },
                "resolvedDependencies": [{"uri": f"git+{repo}@refs/tags/{tag}", "digest": {"gitCommit": sha}}],
            },
            "runDetails": {"builder": {"id": REPO_URL, "version": {"release-cli": _release_cli_version()}}},
        },
    }


def _entry(path: Path, kind: str) -> dict[str, Any]:
    return {"name": path.name, "kind": kind, "sha256": _sha256(path), "size": path.stat().st_size}


def _sign_all(cosign: Cosign, pub: Path, out_dir: Path, manifest: dict[str, Any], req: dict[str, Any]) -> list[dict[str, Any]]:
    version = manifest["version"]
    binary = next((a["name"].split(f"_{version}_", 1)[0] for a in manifest["artifacts"] if isinstance(a, dict) and a.get("kind") == "archive"), manifest["app"])
    checksums = out_dir / "checksums.txt"
    if not checksums.is_file():
        raise SignError(f"checksums.txt is missing from {out_dir}")
    attestation = out_dir / f"{binary}_{version}.intoto.jsonl"
    statement_file = cosign.workdir / "statement.json"
    statement_file.write_text(json.dumps(statement(manifest, req), indent=2) + "\n", encoding="utf-8")
    sig = out_dir / SIGNATURE
    for stale in (sig, attestation):
        stale.unlink(missing_ok=True)
    # the statement's subjects are the archives, so it is checked against an archive, never against checksums.txt
    sample = out_dir / next(a["name"] for a in manifest["artifacts"] if isinstance(a, dict) and a.get("kind") == "archive")
    config = cosign.workdir / "signing-config.json"
    config.write_text(json.dumps(SIGNING_CONFIG), encoding="utf-8")
    cosign.run("sign-blob", "--key", str(cosign.key), "--signing-config", str(config), "--bundle", str(sig), "--yes", str(checksums))
    cosign.run("attest-blob", "--key", str(cosign.key), "--signing-config", str(config), "--statement", str(statement_file), "--bundle", str(attestation), "--yes", str(sample))
    shutil.copyfile(pub, out_dir / PUBLIC_KEY_NAME)
    # a wrong password is caught above; a public key that is not this key's pair is caught here
    cosign.run("verify-blob", "--key", str(pub), "--bundle", str(sig), "--insecure-ignore-tlog", str(checksums))
    cosign.run("verify-blob-attestation", "--key", str(pub), "--bundle", str(attestation), "--type", "slsaprovenance1", "--insecure-ignore-tlog", str(sample))
    return [_entry(sig, "signature"), _entry(attestation, "provenance"), _entry(out_dir / PUBLIC_KEY_NAME, "publickey")]


def _update_manifest(out_dir: Path, added: list[dict[str, Any]]) -> None:
    path = out_dir / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        names = {e["name"] for e in added}
        data["artifacts"] = [a for a in data["artifacts"] if a.get("name") not in names] + added
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SignError(f"could not add the signed files to {path}: {exc}") from exc
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _fail(error: str, **result: Any) -> dict[str, Any]:
    return {"protocol": 1, "ok": False, "error": error, "result": result}


def run(req: dict[str, Any]) -> dict[str, Any]:
    exe = shutil.which("cosign")
    if exe is None:
        return _fail(COSIGN_MISSING)
    dry = bool(req.get("dry_run"))
    root = Path(req["repo_root"])
    try:
        spec = _settings(req)
        manifest = _manifest(req)
        out_dir = Path(manifest.get("dir") or "")
        if not out_dir.is_dir():
            raise SignError(f"the go-build output folder {out_dir} is missing")
        with tempfile.TemporaryDirectory(prefix="sign-") as tmp:
            work = Path(tmp)
            hide: list[str] = []
            if dry:
                print("sign: dry run, signing with a throwaway key", file=sys.stderr)
                password = "dry-run"
                cosign = Cosign(exe, work, work / "cosign.key", password, [password])
                cosign.run("generate-key-pair")
                pub = work / "cosign.pub"
            else:
                pub = (root / spec["public_key"]).resolve()
                if root.resolve() not in pub.parents or not pub.is_file():
                    raise SignError(f"the public key {spec['public_key']} is not in the repo; create the pair with `cosign generate-key-pair`, commit cosign.pub and set `public_key` under [connectors.sign]")
                key = secrets.get(spec["key_secret"])
                if not key:
                    raise SignError(f"secret {spec['key_secret']!r} is not set; run `release secret set {spec['key_secret']}`")
                password = secrets.get(spec["password_secret"])
                if not password:
                    raise SignError(f"secret {spec['password_secret']!r} is not set; run `release secret set {spec['password_secret']}`")
                pem = key_pem(key)
                hide = [key, pem, password, *pem.strip().splitlines()]
                key_file = work / "cosign.key"
                _write_private(key_file, pem)
                cosign = Cosign(exe, work, key_file, password, hide)
            try:
                added = _sign_all(cosign, pub, out_dir, manifest, req)
            finally:
                (work / "cosign.key").unlink(missing_ok=True)
        _update_manifest(out_dir, added)
    except (SignError, secrets.SecretError, OSError) as exc:
        return _fail(str(exc))
    subjects = ["checksums.txt", *(a["name"] for a in manifest["artifacts"] if isinstance(a, dict) and a.get("kind") == "archive")]
    print(f"sign: {len(added)} files added in {out_dir}", file=sys.stderr)
    return {"protocol": 1, "ok": True, "result": {"signed": subjects, "public_key": PUBLIC_KEY_NAME, "tlog": False, "artifacts": added}}


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
