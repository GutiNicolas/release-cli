"""release -rc / -fv CLI. argparse so `-rc` is one flag, not Click clustered shorts."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from release_cli import adapters, gitops
from release_cli.adapters.base import AdapterError
from release_cli.config import Config, ConfigError, load
from release_cli.hooks import HookError, run_hooks
from release_cli.init import InitError, initialize
from release_cli.versioning import Bump, PlanError, parse, plan

RED = "\033[0;31m"
GREEN = "\033[0;32m"
NC = "\033[0m"


def _color() -> bool:
    return sys.stderr.isatty() and not os.environ.get("NO_COLOR")


def _paint(color: str, text: str) -> str:
    if not _color():
        return text
    return f"{color}{text}{NC}"


def info(msg: str) -> None:
    print(msg)


def fail(msg: str, code: int = 1) -> None:
    print(_paint(RED, f"ERROR: {msg}"), file=sys.stderr)
    raise SystemExit(code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release",
        description="Create a release candidate or finalize it. Rewrites only the project version field for Maven, Gradle, or sbt.",
    )
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument("-rc", dest="rc", action="store_true", help="create or increment a release candidate")
    mode.add_argument("-fv", dest="fv", action="store_true", help="promote the current RC to a final release")
    bump = parser.add_mutually_exclusive_group()
    bump.add_argument("--major", action="store_true", help="start a new RC series at next major (X+1.0.0-rc0)")
    bump.add_argument("--minor", action="store_true", help="start a new RC series at next minor (default)")
    bump.add_argument("--patch", action="store_true", help="start a new RC series at next patch")
    parser.add_argument(
        "version",
        nargs="?",
        help="explicit X.Y.Z when starting an RC series (mutually exclusive with --major/--minor/--patch)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan; do not write, commit, or run hooks")
    parser.add_argument("-y", "--yes", action="store_true", help="accept [y] on hook prompts; non-interactive init defaults")
    parser.add_argument("--init", action="store_true", help="detect the build tool and write .release (never releases)")
    parser.add_argument("--force", action="store_true", help="with --init, overwrite existing config")
    parser.add_argument("--skip-hooks", action="store_true", help="do not prompt or run configured commands")
    parser.add_argument("--tool", choices=("maven", "gradle", "sbt"), help="with --init, force this build tool")
    return parser


def _bump_from(ns: argparse.Namespace) -> Bump | None:
    if ns.major:
        return "major"
    if ns.minor:
        return "minor"
    if ns.patch:
        return "patch"
    return None


def _preflight_clean(planned_tags: list[str], *, check_remote: bool) -> None:
    try:
        gitops.current_repo()
    except gitops.GitError as exc:
        fail(str(exc))
    dirty = gitops.porcelain()
    if dirty:
        fail(f"working tree is not clean:\n{dirty}")
    for tag in planned_tags:
        if gitops.tag_exists_local(tag):
            fail(f"local tag already exists: {tag}")
        if check_remote:
            try:
                if gitops.remote_tag_exists(tag):
                    fail(f"remote tag already exists: {tag}")
            except gitops.GitError as exc:
                fail(str(exc))


def _print_line_diff(before: str, after: str) -> None:
    old_lines = before.splitlines()
    new_lines = after.splitlines()
    for idx, (old, new) in enumerate(zip(old_lines, new_lines), start=1):
        if old != new:
            info(f"- {idx}: {old}")
            info(f"+ {idx}: {new}")
    if len(new_lines) != len(old_lines):
        info(f"(line count {len(old_lines)} -> {len(new_lines)})")


def _run_init(cwd: Path, ns: argparse.Namespace) -> Config:
    try:
        cfg, dirtied = initialize(
            cwd,
            tool=ns.tool,
            force=ns.force,
            yes=ns.yes,
            log=info,
        )
    except (InitError, AdapterError, ConfigError) as exc:
        fail(str(exc))
    if dirtied:
        info("config written. commit .gitignore and/or release.toml, then re-run `release -rc`")
    return cfg


def _ensure_config(cwd: Path, ns: argparse.Namespace) -> Config | None:
    try:
        cfg = load(cwd)
    except ConfigError as exc:
        fail(str(exc))
    if cfg is not None:
        return cfg
    info("no .release or release.toml; running init")
    cfg = _run_init(cwd, ns)
    try:
        dirty = gitops.porcelain()
    except gitops.GitError:
        return cfg
    if dirty:
        info("working tree changed by init; not releasing in this run")
        return None
    return cfg


def _scm_for(cfg: Config, artifact: str, version: str, *, snapshot: bool) -> str | None:
    if cfg.tool != "maven":
        return None
    if snapshot:
        return "HEAD"
    return f"{artifact}-{version}"


def main(argv: list[str] | None = None) -> None:
    ns = build_parser().parse_args(argv)
    cwd = Path.cwd()
    if ns.force and not ns.init:
        fail("--force is only valid with --init")
    if ns.init:
        _run_init(cwd, ns)
        return
    if not ns.rc and not ns.fv:
        fail("Usage: release {-rc [base_version] | -fv}  or  release --init")

    cfg = _ensure_config(cwd, ns)
    if cfg is None:
        return

    adapter = adapters.get(cfg.tool)
    bump = _bump_from(ns)
    try:
        state = adapter.read(cwd, cfg)
        current = parse(state.version)
        planned = plan(current, final=ns.fv, bump=bump, explicit=ns.version)
    except (AdapterError, PlanError) as exc:
        fail(str(exc))

    release_ver = planned.release.format()
    snapshot_ver = planned.next_snapshot.format()
    artifact = cfg.artifact or state.artifact
    tags = [release_ver, f"{artifact}-{release_ver}"]
    version_path = cwd / cfg.version_file
    original = version_path.read_text(encoding="utf-8") if version_path.is_file() else ""

    info(_paint(GREEN, "DRY-RUN STARTING RELEASE" if ns.dry_run else "STARTING RELEASE"))
    info(f"TOOL: {cfg.tool}")
    info(f"CURRENT VERSION: {current.format()}")
    info(f"PROJECT ARTIFACT: {artifact}")
    info(f"RELEASE VERSION: {release_ver}")
    info(f"NEXT SNAPSHOT: {snapshot_ver}")
    info(f"TAGS: {', '.join(tags)}")
    if cfg.hooks:
        for hook in cfg.hooks:
            info(f"HOOK {hook.when}: {hook.cmd}")
    else:
        info("HOOKS: none")

    if ns.dry_run:
        run_hooks(cfg.hooks, "before", yes=False, skip=False, dry_run=True, log=info, ask=input)
        preview_cfg_files = _preview_write(adapter, cwd, cfg, release_ver, artifact)
        if not preview_cfg_files:
            fail("dry-run produced no version-file changes; aborting")
        for rel, (before, after) in preview_cfg_files.items():
            info(f"--- {rel} (release) ---")
            _print_line_diff(before, after)
        run_hooks(cfg.hooks, "after", yes=False, skip=False, dry_run=True, log=info, ask=input)
        info("no files written")
        return

    _preflight_clean(tags, check_remote=True)

    start: gitops.RepoState | None = None
    tagged: list[str] = []
    changed: list[Path] = []
    try:
        start = gitops.current_repo()
        run_hooks(
            cfg.hooks,
            "before",
            yes=ns.yes,
            skip=ns.skip_hooks,
            dry_run=False,
            log=info,
            ask=input,
        )
        changed = adapter.write(
            cwd,
            cfg,
            release_ver,
            scm_tag=_scm_for(cfg, artifact, release_ver, snapshot=False),
        )
        run_hooks(
            cfg.hooks,
            "after",
            yes=ns.yes,
            skip=ns.skip_hooks,
            dry_run=False,
            log=info,
            ask=input,
        )
        rel_names = [str(path.relative_to(cwd)) for path in changed]
        gitops.commit(rel_names, f"[manual-release] Prepare release {release_ver}")
        for tag in tags:
            gitops.annotate_tag(tag, f"Release {tag}")
            tagged.append(tag)

        adapter.write(
            cwd,
            cfg,
            snapshot_ver,
            scm_tag=_scm_for(cfg, artifact, release_ver, snapshot=True),
        )
        gitops.commit(rel_names, f"[manual-release] Prepare for next iteration ({snapshot_ver})")
        gitops.push_atomic(tags)
    except HookError as exc:
        if version_path.is_file():
            version_path.write_text(original, encoding="utf-8")
        fail(str(exc))
    except (gitops.GitError, OSError, AdapterError) as exc:
        if version_path.is_file():
            version_path.write_text(original, encoding="utf-8")
        if start is not None:
            gitops.reset_hard(start.sha)
        for tag in tagged:
            gitops.delete_local_tag(tag)
        fail(str(exc))

    info(_paint(GREEN, "RELEASE") + f" {artifact}-{release_ver} " + _paint(GREEN, "FINISHED!"))
    if planned.release.rc is None:
        nxt = planned.next_snapshot
        info(
            f"left project at {snapshot_ver} (already released). "
            f"next `release -rc` starts {nxt.major}.{nxt.minor + 1}.0-rc0"
        )


def _preview_write(adapter, cwd: Path, cfg: Config, version: str, artifact: str) -> dict[str, tuple[str, str]]:
    path = cwd / cfg.version_file
    if not path.is_file():
        return {}
    before = path.read_text(encoding="utf-8")
    try:
        adapter.write(cwd, cfg, version, scm_tag=_scm_for(cfg, artifact, version, snapshot=False))
        after = path.read_text(encoding="utf-8")
    finally:
        path.write_text(before, encoding="utf-8")
    if before == after:
        return {}
    return {cfg.version_file: (before, after)}


if __name__ == "__main__":
    main()
