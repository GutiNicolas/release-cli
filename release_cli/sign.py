"""Built-in connector `sign`: sign a release (Go or Rust) with a maintainer key and attach signed SLSA provenance.

Runs after `go-build` (it reads its manifest from `prior`). With `cosign`:
  * signs `checksums.txt` -> `checksums.txt.sig` (a cosign bundle),
  * writes an in-toto statement with a SLSA provenance predicate (subjects: every archive and bare binary with its sha256; builder:
    release-cli and its version, or the configured `builder_id`; source: repo, tag, commit; the build flags) and signs it with `cosign attest-blob`
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
import re
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
SUBJECT_KINDS = ("archive", "binary")
PUBLIC_KEY_NAME = "cosign.pub"
REPO_URL = "https://github.com/GutiNicolas/release-cli"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
BUILD_TYPE = f"{REPO_URL}/go-build/v1"
BUILD_TYPE_SUFFIX = "/go-build/v1"
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
    builder_id = cfg.get("builder_id")
    if builder_id is not None:
        out["builder_id"] = _builder_id(builder_id)
    if cfg.get("tlog", False) is not False:
        raise SignError("`tlog = true` is not supported: `sign` never uploads to a transparency log (leave `tlog` unset or false)")
    return out


def _builder_id(value: Any) -> str:
    """`builder_id` as an https URL without a trailing slash (buildType is `<builder_id>/go-build/v1`)."""
    text = value.strip().rstrip("/") if isinstance(value, str) else ""
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.hostname or any(c.isspace() for c in text):
        raise SignError("`builder_id` under [connectors.sign] must be an https:// URL, for example https://github.com/acme/app (or leave it unset)")
    return text


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


SCP_REMOTE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s@]+):(?P<path>(?!//)[^\s]+)$")


def _source_repository(remote: Any) -> str:
    """The remote as an https URL without credentials (`git@host:o/r.git` and `ssh://` remotes included), so a
    verifier can match it. Raises when no repository can be derived: provenance never names an empty source."""
    url = remote.strip() if isinstance(remote, str) else ""
    host = path = ""
    if "://" in url:
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path
        if parts.scheme in ("http", "https") and parts.port:
            host = f"{host}:{parts.port}"  # an ssh port is not the web port, an http(s) one is
    elif (scp := SCP_REMOTE.match(url)) is not None:
        host, path = scp["host"], "/" + scp["path"].lstrip("/")
    path = path.removesuffix("/").removesuffix(".git")
    if not host or len(path.strip("/").split("/")) < 2 or not all(path.strip("/").split("/")):
        raise SignError(f"cannot derive the source repository from the git remote {_redact(url)!r}; set `origin` to the repository URL (for example https://github.com/<owner>/<repo>)")
    return f"https://{host}{path}"


def _redact(url: str) -> str:
    return re.sub(r"//[^@/]*@", "//", url) if url else "(none)"


def _release_cli_version() -> str:
    try:
        return metadata.version("release-cli")
    except metadata.PackageNotFoundError:
        return "unknown"


def statement(manifest: dict[str, Any], req: dict[str, Any], builder_id: str | None = None) -> dict[str, Any]:
    """The in-toto statement: SLSA provenance for every archive and bare binary of the manifest. With `builder_id` the
    builder is that URL, the build type hangs off it and no tool name or version appears; without it the builder is
    release-cli."""
    archives = sorted(_subjects(manifest), key=lambda a: a["name"])
    if not archives:
        raise SignError("the build manifest lists no archive to attest")
    tag, sha = manifest.get("tag") or tag_of(req), manifest["commit"]
    repo = _source_repository(req.get("remote_url"))
    if builder_id:
        build_type, builder = f"{builder_id}{BUILD_TYPE_SUFFIX}", {"id": builder_id}
    else:
        build_type, builder = BUILD_TYPE, {"id": REPO_URL, "version": {"release-cli": _release_cli_version()}}
    return {
        "_type": STATEMENT_TYPE,
        "subject": [{"name": a["name"], "digest": {"sha256": a["sha256"]}} for a in archives],
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "buildDefinition": {
                "buildType": build_type,
                "externalParameters": {
                    "source": {"repository": repo, "tag": tag, "commit": sha},
                    "build": manifest.get("build") or {},
                },
                "resolvedDependencies": [{"uri": f"git+{repo}@refs/tags/{tag}", "digest": {"gitCommit": sha}}],
            },
            "runDetails": {"builder": builder},
        },
    }


def _subjects(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """What people download, so what is signed, checked on disk and attested: the `archive` and `binary` entries."""
    return [a for a in manifest["artifacts"] if isinstance(a, dict) and a.get("kind") in SUBJECT_KINDS]


def _entry(path: Path, kind: str) -> dict[str, Any]:
    return {"name": path.name, "kind": kind, "sha256": _sha256(path), "size": path.stat().st_size}


def _check_on_disk(out_dir: Path, manifest: dict[str, Any]) -> None:
    """Fail closed unless every archive and binary on disk still has the digest the build recorded and checksums.txt lists."""
    checksums = out_dir / "checksums.txt"
    if not checksums.is_file():
        raise SignError(f"checksums.txt is missing from {out_dir}")
    listed: dict[str, str] = {}
    for line in checksums.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if name:
            listed[name] = digest
    for entry in manifest["artifacts"]:
        if not isinstance(entry, dict) or entry.get("kind") not in (*SUBJECT_KINDS, "checksums"):
            continue
        path = out_dir / entry["name"]
        if not path.is_file():
            raise SignError(f"{entry['name']} is missing from {out_dir}; run the build connector again")
        actual = _sha256(path)
        if actual != entry.get("sha256"):
            raise SignError(f"{entry['name']} changed since the build (sha256 {actual[:12]}... on disk, manifest says {str(entry.get('sha256'))[:12]}...); run the build connector again")
        if entry["kind"] in SUBJECT_KINDS and listed.get(entry["name"]) != actual:
            raise SignError(f"checksums.txt does not list the sha256 of {entry['name']} that is on disk; run the build connector again")


def _sign_all(cosign: Cosign, pub: Path, out_dir: Path, manifest: dict[str, Any], req: dict[str, Any], builder_id: str | None = None) -> list[dict[str, Any]]:
    version = manifest["version"]
    binary = next((a["name"].split(f"_{version}_", 1)[0] for a in manifest["artifacts"] if isinstance(a, dict) and a.get("kind") == "archive"), manifest["app"])
    _check_on_disk(out_dir, manifest)
    checksums = out_dir / "checksums.txt"
    attestation = out_dir / f"{binary}_{version}.intoto.jsonl"
    statement_file = cosign.workdir / "statement.json"
    statement_file.write_text(json.dumps(statement(manifest, req, builder_id), indent=2) + "\n", encoding="utf-8")
    sig = out_dir / SIGNATURE
    for stale in (sig, attestation):
        stale.unlink(missing_ok=True)
    # the statement's subjects are the archives, so it is checked against an archive, never against checksums.txt
    subjects = _subjects(manifest)
    sample = out_dir / next((a["name"] for a in subjects if a["kind"] == "archive"), subjects[0]["name"])
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
                added = _sign_all(cosign, pub, out_dir, manifest, req, spec.get("builder_id"))
            finally:
                (work / "cosign.key").unlink(missing_ok=True)
        _update_manifest(out_dir, added)
    except (SignError, secrets.SecretError, OSError) as exc:
        return _fail(str(exc))
    subjects = ["checksums.txt", *(a["name"] for a in _subjects(manifest))]
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
