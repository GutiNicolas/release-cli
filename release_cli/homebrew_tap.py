"""Built-in connector `homebrew-tap`: a Homebrew formula for a final release, committed to a tap repository.

The formula is rendered from the `go-build` manifest (`prior["go-build"]`): one url and sha256 per os/arch, `bin.install`
and a `test do` that runs `<binary> version`. Release candidates are skipped (Homebrew carries finals only). A real run
commits `Formula/<binary>.rb` to the tap with `gh api` and the token stored under `token_secret`; a dry run only renders
the formula into `out_dir`.

    python -m release_cli.homebrew_tap questions|run   (protocol 1 on stdin/stdout)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from release_cli import secrets
from release_cli.go_build import download_base, github_slug
from release_cli.versioning import parse

DEFAULT_SECRET = "homebrew-tap-token"
TAP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")
GH_MISSING = "the GitHub CLI `gh` is not on PATH; install it (https://cli.github.com)"
TIMEOUT = 60
# Go os -> Homebrew on_<os> block, and Go arch -> Homebrew cpu block
PLATFORMS = [("darwin", "macos"), ("linux", "linux")]
CPUS = [("arm64", "on_arm"), ("amd64", "on_intel")]


class TapError(RuntimeError):
    pass


def questions(req: dict[str, Any]) -> dict[str, Any]:
    if github_slug(req.get("remote_url")) is None:
        return {"protocol": 1, "applies": False, "reason": "the remote is not on github.com (the formula points at its releases)", "questions": []}
    return {"protocol": 1, "dry_run": "safe", "questions": []}


def _rb(text: str) -> str:
    """A Ruby double-quoted string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("#", "\\#") + '"'


def _class_name(binary: str) -> str:
    name = "".join(part[:1].upper() + part[1:] for part in re.split(r"[-_.]", binary) if part)
    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name):
        raise TapError(f"cannot make a Homebrew formula name from the binary {binary!r}: it must start with a letter")
    return name


def render_formula(manifest: dict[str, Any], binary: str, base: str, homepage: str, desc: str) -> str:
    archives = {(a.get("os"), a.get("arch")): a for a in manifest.get("artifacts") or [] if a.get("kind") == "archive"}
    blocks: list[str] = []
    for goos, brew_os in PLATFORMS:
        cpus = [(cpu, goarch) for goarch, cpu in CPUS if (goos, goarch) in archives]
        if not cpus:
            continue
        blocks.append(f"  on_{brew_os} do")
        for cpu, goarch in cpus:
            archive = archives[(goos, goarch)]
            url = f"{base}/{archive['name']}"
            blocks += [f"    {cpu} do", f"      url {_rb(url)}", f"      sha256 {_rb(archive['sha256'])}", "    end"]
        blocks.append("  end\n")
    if not blocks:
        raise TapError("the go-build manifest has no darwin or linux archive to put in a formula")
    lines = [
        f"class {_class_name(binary)} < Formula",
        f"  desc {_rb(desc)}",
        f"  homepage {_rb(homepage)}",
        f"  version {_rb(manifest['version'])}",
        "",
        *blocks,
        "  def install",
        f"    bin.install {_rb(binary)}",
        "  end",
        "",
        "  test do",
        f"    assert_match version.to_s, shell_output(\"#{{bin}}/{binary} version\")",
        "  end",
        "end",
    ]
    return "\n".join(lines) + "\n"


def _settings(req: dict[str, Any]) -> dict[str, Any]:
    cfg = req.get("config") or {}
    tap = cfg.get("tap")
    if not isinstance(tap, str) or not TAP_RE.match(tap):
        raise TapError('set `tap = "owner/homebrew-tap"` under [connectors.homebrew-tap]')
    secret = cfg.get("token_secret", DEFAULT_SECRET)
    if not isinstance(secret, str):
        raise TapError("`token_secret` must be the name of a secret")
    desc = cfg.get("desc")
    return {"tap": tap, "secret": secret, "desc": desc if isinstance(desc, str) and desc else None}


def _manifest(req: dict[str, Any]) -> dict[str, Any]:
    entry = (req.get("prior") or {}).get("go-build")
    if not isinstance(entry, dict):
        raise TapError("go-build has not run before this connector; put go-build before homebrew-tap in the chain")
    if entry.get("status") != "ok" or not isinstance(entry.get("result"), dict):
        raise TapError("go-build did not succeed, so there is nothing to put in the formula")
    return entry["result"]


def _record(directory: Path, name: str, formula: Path) -> None:
    """List the formula in the manifest on disk (re-read, so files other connectors added are kept)."""
    path = directory / "manifest.json"
    if not path.is_file():
        return
    manifest = json.loads(path.read_text(encoding="utf-8"))
    sha = hashlib.sha256(formula.read_bytes()).hexdigest()
    entry = {"name": name, "kind": "formula", "sha256": sha, "size": formula.stat().st_size}
    manifest["artifacts"] = [a for a in manifest.get("artifacts", []) if a.get("name") != name] + [entry]
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _gh(gh: str, token: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GH_TOKEN": token, "GH_PROMPT_DISABLED": "1"}
    return subprocess.run([gh, *args], capture_output=True, text=True, check=False, env=env, timeout=TIMEOUT)


def _publish(tap: str, binary: str, version: str, formula: str, token: str) -> tuple[str | None, str | None]:
    """Commit the formula to the tap. Returns (commit sha, None), or (None, reason) when the tap already has it."""
    gh = shutil.which("gh")
    if gh is None:
        raise TapError(GH_MISSING)
    path = f"repos/{tap}/contents/Formula/{binary}.rb"
    current = _gh(gh, token, "api", path)
    sha: str | None = None
    if current.returncode == 0:
        found = json.loads(current.stdout)
        sha = found.get("sha")
        if base64.b64decode(found.get("content") or "").decode("utf-8", "replace") == formula:
            return None, "the tap already has this formula"
    elif "404" not in (current.stderr + current.stdout):
        raise TapError(f"could not read {tap}: {(current.stderr or current.stdout).strip() or f'gh exited {current.returncode}'}")
    args = ["api", "--method", "PUT", path, "-f", f"message={binary} {version}", "-f", f"content={base64.b64encode(formula.encode()).decode()}"]
    if sha:
        args += ["-f", f"sha={sha}"]
    args += ["--jq", ".commit.sha"]
    put = _gh(gh, token, *args)
    if put.returncode != 0:
        raise TapError(f"could not commit the formula to {tap}: {(put.stderr or put.stdout).strip() or f'gh exited {put.returncode}'}")
    return put.stdout.strip() or None, None


def _fail(error: str, **result: Any) -> dict[str, Any]:
    return {"protocol": 1, "ok": False, "error": error, "result": result}


def _skip(reason: str) -> dict[str, Any]:
    return {"protocol": 1, "ok": True, "result": {"formula": None, "skipped_reason": reason}}


def run(req: dict[str, Any]) -> dict[str, Any]:
    version = req["release_version"]
    if parse(version).rc is not None:
        return _skip(f"{version} is a release candidate; Homebrew carries final releases only")
    try:
        spec = _settings(req)
        manifest = _manifest(req)
        binary = _binary_of(manifest)
        slug = github_slug(req.get("remote_url"))
        base = download_base(req)
        if slug is None or base is None:
            raise TapError("the remote is not on github.com, so the formula has no release url to point at")
        formula = render_formula(manifest, binary, base, f"https://github.com/{slug[0]}/{slug[1]}", spec["desc"] or f"{binary} command line tool")
        dry = bool(req.get("dry_run"))
        directory = Path(req["out_dir"]) if dry and req.get("out_dir") else Path(manifest["dir"])
        if dry and not req.get("out_dir"):
            raise TapError("dry run without an out_dir to write into")
        directory.mkdir(parents=True, exist_ok=True)
        formula_file = directory / f"{binary}.rb"
        formula_file.write_text(formula, encoding="utf-8")
        _record(directory, formula_file.name, formula_file)
        result: dict[str, Any] = {"formula": formula_file.name, "path": f"Formula/{binary}.rb", "tap": spec["tap"]}
        if dry:
            print(f"homebrew-tap: rendered {formula_file} (dry run, nothing committed to {spec['tap']})", file=sys.stderr)
            return {"protocol": 1, "ok": True, "result": result}
        token = secrets.get(spec["secret"])
        if not token:
            raise TapError(f"secret {spec['secret']!r} is not set; run `release secret set {spec['secret']}`")
        commit, reason = _publish(spec["tap"], binary, version, formula, token)
    except (TapError, secrets.SecretError, OSError, subprocess.SubprocessError, ValueError) as exc:
        return _fail(str(exc))
    if commit:
        result["commit"] = commit
        print(f"homebrew-tap: committed Formula/{binary}.rb to {spec['tap']} ({commit[:7]})", file=sys.stderr)
    if reason:
        result["skipped_reason"] = reason
    return {"protocol": 1, "ok": True, "result": result}


def _binary_of(manifest: dict[str, Any]) -> str:
    """The binary's name, read back from an archive name (`<binary>_<version>_<os>_<arch>.tar.gz`)."""
    for a in manifest.get("artifacts") or []:
        suffix = f"_{manifest['version']}_{a.get('os')}_{a.get('arch')}.tar.gz"
        if a.get("kind") == "archive" and a["name"].endswith(suffix):
            return a["name"][: -len(suffix)]
    raise TapError("the go-build manifest lists no archive to read the binary name from")


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
