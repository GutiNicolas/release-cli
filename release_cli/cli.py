"""release -rc / -fv CLI. argparse so `-rc` is one flag, not Click clustered shorts."""

from __future__ import annotations

import argparse
import sys
from importlib import metadata
from pathlib import Path

from release_cli import adapters, commands, gitops, prompts
from release_cli import connectors as cx
from release_cli.adapters.base import AdapterError
from release_cli.config import Config, ConfigError, load
from release_cli.connectors import ChainInterrupted, ConnectorError
from release_cli.hooks import HookError, run_hooks
from release_cli.init import InitError, initialize
from release_cli.prompts import PromptError
from release_cli.ui import (
    GREEN,
    _paint,
    emit,
    fail,
    info,
    reset_mode,
    result,
    set_mode,
)
from release_cli.versioning import (
    Bump,
    PlanError,
    ProjectVersion,
    ReleasePlan,
    parse,
    plan,
)

HELP_EPILOG = """\
hooks vs connectors:
  hook       before the push. A shell command of this repo (mvn, gradle, sbt, ...) in .release.
             Add with `release hook add`, edit with `release edit`. A failing hook rolls back:
             version file restored, local commits reset, local tags deleted. Nothing was pushed.
  connector  after the push. A Python program (protocol 1, JSON over stdin/stdout) installed with
             `release connector add <github-url>[@ref]`. Runs in order; results pass to the next.
             A connector NEVER deletes, moves, or re-creates the tag. Once the push succeeds the tag stays.

resume (state in ~/.local/share/release/runs/{repo}/{tag}.json):
  build failed          fix it, then: release connectors run <tag>
  deploy failed         release connectors run <tag> --from platform-deploy   (reuses the saved build result)
  Ctrl-C during a poll  release connectors run <tag> --from <connector>        (re-polls the saved job_id)
  one connector only    release connectors run <tag> --only cursor-review
  what happened         release connectors status <tag>

commands:
  release connector add|ls|update|remove   release connectors run|status <tag>
  release hook add --when before|after --cmd "..."   release edit
  release status [--json]   release notes [tag]   release undo <rc-tag>
  release update   (pull the release-cli clone and reinstall, from any directory)
  release doctor [--json]   (why release would not work here; changes nothing)

CI: --defaults with --json (one JSON object on stdout) or --quiet (result line only).
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release",
        description="Create a release candidate or finalize it. Rewrites only the project version field for Maven, Gradle, or sbt.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    parser.add_argument(
        "--defaults",
        action="store_true",
        help="take the configured default on every hook, connector, and --init question (never assumes yes)",
    )
    parser.add_argument("--init", action="store_true", help="detect the build tool and write .release (never releases)")
    parser.add_argument("--force", action="store_true", help="with --init, overwrite existing config")
    parser.add_argument("--skip-hooks", action="store_true", help="do not prompt or run hooks (connectors still run)")
    parser.add_argument("--skip-connectors", action="store_true", help="do not run connectors after the push")
    parser.add_argument("--tool", choices=("maven", "gradle", "sbt"), help="with --init, force this build tool")
    out = parser.add_mutually_exclusive_group()
    out.add_argument("-q", "--quiet", action="store_true", help="print only the result line and errors (hook output goes to stderr)")
    out.add_argument("--json", action="store_true", help="print one JSON object on stdout (also on errors); everything else on stderr")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {_version()}")
    return parser


def _version() -> str:
    try:
        return metadata.version("release-cli")
    except metadata.PackageNotFoundError:
        return "unknown"


def _bump_from(ns: argparse.Namespace) -> Bump | None:
    if ns.major:
        return "major"
    if ns.minor:
        return "minor"
    if ns.patch:
        return "patch"
    return None


def _plan_release(current: ProjectVersion, ns: argparse.Namespace, bump: Bump | None) -> ReleasePlan:
    """Plan the release. A new series while an RC is open asks before replacing it."""
    override = False
    if not ns.fv and current.rc is not None and (bump or ns.version):
        preview = plan(current, final=False, bump=bump, explicit=ns.version, override=True)
        override = prompts.ask_bool(
            f"a release candidate is already in progress ({current.format()}). "
            f"overwrite it with {preview.release.format()}?",
            False,
            defaults=ns.defaults,
        )
    return plan(current, final=ns.fv, bump=bump, explicit=ns.version, override=override)


def _preflight(planned_tags: list[str]) -> list[str]:
    """Everything that would make the release fail after the hooks ran. Empty means go."""
    try:
        repo = gitops.current_repo()
    except gitops.GitError as exc:
        return [str(exc)]
    problems: list[str] = []
    dirty = gitops.porcelain()
    if dirty:
        problems.append(f"working tree is not clean:\n{dirty}")
    problems += [f"local tag already exists: {tag}" for tag in planned_tags if gitops.tag_exists_local(tag)]
    try:
        gitops.fetch_branch(repo.branch)
        late = gitops.behind(repo.branch)
        if late:
            problems.append(
                f"{repo.branch} is {late} commit(s) behind origin/{repo.branch}; pull first "
                "(otherwise the push fails after the hooks already ran, e.g. after a deploy)"
            )
        problems += [f"remote tag already exists: {tag}" for tag in planned_tags if gitops.remote_tag_exists(tag)]
    except gitops.GitError as exc:
        problems.append(f"cannot check origin: {exc}")
    return problems


def _print_line_diff(before: str, after: str) -> None:
    old_lines = before.splitlines()
    new_lines = after.splitlines()
    for idx, (old, new) in enumerate(zip(old_lines, new_lines, strict=False), start=1):
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
            defaults=ns.defaults,
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
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in commands.COMMANDS:
        commands.main(argv)
        return
    ns = build_parser().parse_args(argv)
    set_mode(quiet=ns.quiet, as_json=ns.json)
    try:
        _release(ns)
    finally:
        reset_mode()


def _release(ns: argparse.Namespace) -> None:
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
        planned = _plan_release(current, ns, bump)
    except (AdapterError, PlanError) as exc:
        fail(str(exc))
    except EOFError:
        fail("no answer (stdin closed); run with --defaults to take each default")

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

    summary = {"tool": cfg.tool, "artifact": artifact, "version": release_ver, "next_snapshot": snapshot_ver, "tags": tags}
    if ns.dry_run:
        run_hooks(cfg.hooks, "before", defaults=False, skip=False, dry_run=True, log=info, ask=input)
        preview_cfg_files = _preview_write(adapter, cwd, cfg, release_ver, artifact)
        if not preview_cfg_files:
            fail("dry-run produced no version-file changes; aborting")
        for rel, (before, after) in preview_cfg_files.items():
            info(f"--- {rel} (release) ---")
            _print_line_diff(before, after)
        run_hooks(cfg.hooks, "after", defaults=False, skip=False, dry_run=True, log=info, ask=input)
        if not ns.skip_connectors:
            try:
                head = gitops.current_repo().sha
            except gitops.GitError:
                head = "HEAD"
            _post_push(cwd, cfg, release_ver, head, ns, dry_run=True)
        info("no files written")
        problems = _preflight(tags)
        if problems:
            fail("dry-run: the release would stop before the hooks:\n- " + "\n- ".join(problems))
        info("PREFLIGHT: ok (clean tree, tags free, up to date with origin)")
        result(f"dry-run ok: {artifact} {release_ver} (tags {', '.join(tags)})")
        emit({**summary, "ok": True, "dry_run": True, "files": sorted(preview_cfg_files)})
        return

    problems = _preflight(tags)
    if problems:
        fail("\n- ".join(["cannot release:", *problems]))

    start: gitops.RepoState | None = None
    tagged: list[str] = []
    pushing = False
    try:
        start = gitops.current_repo()
        run_hooks(
            cfg.hooks,
            "before",
            defaults=ns.defaults,
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
            defaults=ns.defaults,
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
        pushing = True
        gitops.push_atomic(tags)
    except BaseException as exc:
        # A failed --atomic push leaves nothing on origin. Ctrl-C while pushing may not: never roll back then.
        if pushing and isinstance(exc, KeyboardInterrupt):
            on_origin = _on_origin(tags[0])
            if on_origin:
                fail(f"interrupted after the push: {release_ver} is released. run the connectors: release connectors run {release_ver}", 130)
            if on_origin is None:
                fail(
                    f"interrupted during the push and origin cannot be checked; {tags[0]} may already be there, so nothing "
                    "was rolled back. check `git ls-remote --tags origin`, then push or delete the local commits and tags",
                    130,
                )
        _rollback(version_path, original, start, tagged)
        if isinstance(exc, KeyboardInterrupt):
            fail("interrupted; release rolled back (nothing was pushed to git)", 130)
        if isinstance(exc, EOFError):
            fail("no answer (stdin closed); rolled back. run with --defaults to take each default")
        if isinstance(exc, (HookError, gitops.GitError, OSError, AdapterError, PromptError)):
            fail(f"{exc} (rolled back; nothing was pushed to git)")
        raise

    result(_paint(GREEN, "RELEASE") + f" {artifact}-{release_ver} " + _paint(GREEN, "FINISHED!"))
    summary["sha"] = gitops.tag_commit(release_ver)
    if planned.release.rc is None:
        nxt = planned.next_snapshot
        info(
            f"left project at {snapshot_ver} (already released). "
            f"next `release -rc` starts {nxt.major}.{nxt.minor + 1}.0-rc0"
        )

    # Past the atomic push: nothing below may reset, delete, or move a tag.
    if ns.skip_connectors:
        info("connectors skipped (--skip-connectors)")
        emit({**summary, "ok": True, "dry_run": False, "connectors": {}})
        return
    ok = _post_push(cwd, cfg, release_ver, summary["sha"], ns, dry_run=False)
    saved = cx_state(cwd, release_ver)
    if not ok:
        fail(
            f"release {release_ver} is pushed and its tags stay; a connector did not finish (see above to resume)",
            pushed=True, connectors=saved, **summary,
        )
    emit({**summary, "ok": True, "dry_run": False, "connectors": saved})


def _on_origin(tag: str) -> bool | None:
    """True/False when origin answers, None when it cannot be checked."""
    try:
        return gitops.remote_tag_exists(tag)
    except gitops.GitError:
        return None


def _rollback(version_path: Path, original: str, start: gitops.RepoState | None, tagged: list[str]) -> None:
    """Undo everything local before the push: tags, release commits, the version file."""
    for tag in tagged:
        gitops.delete_local_tag(tag)
    if start is not None:
        try:
            gitops.reset_hard(start.sha)
            return
        except gitops.GitError as exc:
            print(f"rollback: git reset --hard {start.sha} failed: {exc}", file=sys.stderr)
    if version_path.is_file():
        version_path.write_text(original, encoding="utf-8")


def cx_state(cwd: Path, release_ver: str) -> dict[str, str]:
    """name -> status of each connector run saved for this release."""
    state = cx.load_state(cx.repo_name(gitops.remote_url(), cwd), release_ver) or {}
    return {name: entry.get("status") for name, entry in state.get("connectors", {}).items()}


def _post_push(cwd: Path, cfg: Config, release_ver: str, sha: str, ns: argparse.Namespace, *, dry_run: bool) -> bool:
    try:
        return commands.post_push(cwd, cfg, release_ver, sha, defaults=ns.defaults, dry_run=dry_run)
    except (ChainInterrupted, KeyboardInterrupt):
        fail(f"interrupted. the tags stay pushed. see: release connectors status {release_ver}", 130, pushed=True, version=release_ver)
    except (ConnectorError, ConfigError) as exc:
        fail(f"{exc} (release {release_ver} tags are not touched)")


def _preview_write(adapter, cwd: Path, cfg: Config, version: str, artifact: str) -> dict[str, tuple[str, str]]:
    """Write the release version, read every touched file, put everything back byte for byte."""
    before = {path: path.read_bytes() for path in adapter.paths(cwd, cfg) if path.is_file()}
    if not before:
        return {}
    try:
        adapter.write(cwd, cfg, version, scm_tag=_scm_for(cfg, artifact, version, snapshot=False))
        after = {path: path.read_bytes() for path in before}
    finally:
        for path, data in before.items():
            path.write_bytes(data)
    return {
        str(path.relative_to(cwd)): (before[path].decode("utf-8"), after[path].decode("utf-8"))
        for path in before
        if before[path] != after[path]
    }


if __name__ == "__main__":
    main()
