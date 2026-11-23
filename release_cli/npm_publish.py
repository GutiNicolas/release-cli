"""Built-in connector `npm-publish`: publish the app's npm package with the same version as the binary.

The package folder (`package_dir`) is copied to a temp dir, its `version` is set there (the tracked `package.json`
is never edited), `npm pack` writes the tarball into the `go-build` output folder and `npm publish <tarball>` uploads
it. RCs go under the dist-tag `rc_dist_tag` (default `next`), finals under `latest`. Dry run: `npm publish --dry-run`.
A version that is already on the registry is a success with `published: false`, so a resumed run does not fail.

The token is the secret `npm-token` (or the one named by `token_secret`), written to a temp `.npmrc` and never
logged; without that secret npm uses the user's own `npm login`.

    python -m release_cli.npm_publish questions|run   (protocol 1 on stdin/stdout)
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from release_cli import secrets

DEFAULT_SECRET = "npm-token"
DEFAULT_REGISTRY = "https://registry.npmjs.org/"
ACCESS = ("public", "restricted")
NPM_MISSING = "`npm` is not on PATH; install Node.js (https://nodejs.org) and make sure `npm --version` works in this shell"
ERROR_TAIL = 15
TIMEOUT = 600
NO_COPY = ("node_modules", ".git")


class PublishError(RuntimeError):
    pass


def questions(req: dict[str, Any]) -> dict[str, Any]:
    return {"protocol": 1, "dry_run": "safe", "questions": []}


def _settings(req: dict[str, Any]) -> dict[str, Any]:
    cfg = req.get("config") or {}
    package_dir = cfg.get("package_dir")
    access = cfg.get("access", "public")
    rc_tag = cfg.get("rc_dist_tag", "next")
    registry = cfg.get("registry", DEFAULT_REGISTRY)
    token_secret = cfg.get("token_secret")
    if not isinstance(package_dir, str) or not package_dir:
        raise PublishError('set `package_dir` under [connectors.npm-publish], for example "packages/app"')
    if access not in ACCESS:
        raise PublishError('`access` must be "public" or "restricted"')
    if not isinstance(rc_tag, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", rc_tag) or rc_tag == "latest":
        raise PublishError('`rc_dist_tag` must be a tag name other than "latest", for example "next"')
    if not isinstance(registry, str) or urlsplit(registry).scheme not in ("http", "https") or not urlsplit(registry).netloc:
        raise PublishError("`registry` must be a URL")
    if token_secret is not None and not isinstance(token_secret, str):
        raise PublishError("`token_secret` must be the name of a secret")
    return {"package_dir": package_dir, "access": access, "rc_tag": rc_tag, "registry": registry, "token_secret": token_secret}


def _fail(error: str, **result: Any) -> dict[str, Any]:
    return {"protocol": 1, "ok": False, "error": error, "result": result}


def _tail(text: str, token: str | None) -> str:
    out = "\n".join(text.strip().splitlines()[-ERROR_TAIL:])
    return out.replace(token, "***") if token else out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _npm(args: list[str], cwd: Path, token: str | None) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(["npm", *args], cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise PublishError(f"`npm {args[0]}` timed out after {TIMEOUT} s") from exc
    except OSError as exc:
        raise PublishError(f"could not run npm: {exc}") from exc
    if token:  # npm may echo its config on a failure: the token never leaves this function
        proc.stdout, proc.stderr = proc.stdout.replace(token, "***"), proc.stderr.replace(token, "***")
    return proc


def _token(settings: dict[str, Any]) -> str | None:
    """The configured secret must exist; the default one is optional (the user may be logged in with `npm login`)."""
    explicit = settings["token_secret"]
    name = explicit or DEFAULT_SECRET
    try:
        value = secrets.get(name)
    except secrets.SecretError as exc:
        raise PublishError(str(exc)) from exc
    if not value and explicit:
        raise PublishError(f"secret {name!r} is not set; run `release secret set {name}`")
    return value


def _prepare(root: Path, settings: dict[str, Any], version: str, tmp: Path) -> Path:
    src = (root / settings["package_dir"]).resolve()
    if root.resolve() not in src.parents or not (src / "package.json").is_file():
        raise PublishError(f"no package.json in {settings['package_dir']!r} (package_dir is relative to the repo root)")
    copy = tmp / "package"
    shutil.copytree(src, copy, ignore=shutil.ignore_patterns(*NO_COPY))
    pkg_file = copy / "package.json"
    try:
        pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise PublishError(f"{settings['package_dir']}/package.json is not valid JSON: {exc}") from exc
    if not isinstance(pkg, dict) or not isinstance(pkg.get("name"), str) or not pkg["name"]:
        raise PublishError(f"{settings['package_dir']}/package.json has no `name`")
    pkg["version"] = version  # only the copy changes; the tracked file keeps its own version
    pkg_file.write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
    return copy


def _published(name: str, version: str, registry: str, cwd: Path, token: str | None, userconfig: Path | None) -> bool:
    args = ["view", f"{name}@{version}", "version", "--registry", registry]
    if userconfig:
        args += ["--userconfig", str(userconfig)]
    proc = _npm(args, cwd, token)
    if proc.returncode == 0:
        return proc.stdout.strip() == version
    if "E404" in proc.stderr or "404" in proc.stderr:
        return False
    raise PublishError(f"could not check the registry for {name}@{version}: {_tail(proc.stderr or proc.stdout, token)}")


def _add_to_manifest(dest: Path, tgz: Path) -> dict[str, Any]:
    entry = {"name": tgz.name, "kind": "npm", "sha256": _sha256(tgz), "size": tgz.stat().st_size}
    manifest_file = dest / "manifest.json"
    if manifest_file.is_file():
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["artifacts"] = [a for a in manifest.get("artifacts", []) if a.get("name") != tgz.name] + [entry]
        manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return entry


def _publish(req: dict[str, Any], settings: dict[str, Any], dest: Path) -> dict[str, Any]:
    version = req["release_version"]
    dry = bool(req.get("dry_run"))
    dist_tag = settings["rc_tag"] if "-" in version else "latest"
    token = _token(settings)
    with tempfile.TemporaryDirectory(prefix="npm-publish-") as tmpdir:
        tmp = Path(tmpdir)
        pkg = _prepare(Path(req["repo_root"]), settings, version, tmp)
        name = json.loads((pkg / "package.json").read_text(encoding="utf-8"))["name"]
        userconfig: Path | None = None
        if token:
            userconfig = tmp / ".npmrc"
            parts = urlsplit(settings["registry"])
            userconfig.write_text(f"//{parts.netloc}{parts.path.rstrip('/')}/:_authToken={token}\n", encoding="utf-8")
            userconfig.chmod(0o600)
        auth = ["--userconfig", str(userconfig)] if userconfig else []
        result: dict[str, Any] = {"name": name, "version": version, "dist_tag": dist_tag, "published": False}
        if not dry and _published(name, version, settings["registry"], pkg, token, userconfig):
            print(f"npm-publish: {name}@{version} is already on the registry; nothing to publish", file=sys.stderr)
            return result
        dest.mkdir(parents=True, exist_ok=True)
        packed = _npm(["pack", str(pkg), "--pack-destination", str(dest), "--json"], tmp, token)
        if packed.returncode != 0:
            raise PublishError(f"npm pack failed: {_tail(packed.stderr or packed.stdout, token)}")
        try:
            tgz = dest / json.loads(packed.stdout)[0]["filename"]
        except (ValueError, LookupError, TypeError) as exc:
            raise PublishError(f"could not read the tarball name from `npm pack`: {_tail(packed.stdout, token)}") from exc
        if not tgz.is_file():
            raise PublishError(f"`npm pack` did not produce {tgz.name} in {dest}")
        result["artifact"] = _add_to_manifest(dest, tgz)
        args = ["publish", str(tgz), "--tag", dist_tag, "--access", settings["access"], "--registry", settings["registry"], *auth]
        if dry:
            args.append("--dry-run")
        proc = _npm(args, tmp, token)
        if proc.returncode != 0:
            raise PublishError(f"npm publish failed for {name}@{version}: {_tail(proc.stderr or proc.stdout, token)}")
        result["published"] = not dry
        print(f"npm-publish: {name}@{version} {'would be published (dry run)' if dry else 'published'} under `{dist_tag}`", file=sys.stderr)
        return result


def run(req: dict[str, Any]) -> dict[str, Any]:
    if shutil.which("npm") is None:
        return _fail(NPM_MISSING)
    try:
        settings = _settings(req)
        entry = (req.get("prior") or {}).get("go-build")
        manifest = (entry or {}).get("result") or {}
        if not entry or entry.get("status") != "ok" or not manifest.get("dir"):
            raise PublishError("npm-publish packs into the `go-build` output; add `go-build` before it in the order (`release connector ls`)")
        return {"protocol": 1, "ok": True, "result": _publish(req, settings, Path(manifest["dir"]))}
    except (PublishError, OSError) as exc:
        return _fail(str(exc))


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
