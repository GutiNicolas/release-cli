"""Subcommands: connector add|ls|update|remove, connectors run|status, hook add, edit."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

from release_cli import connectors as cx
from release_cli import cursor_review, gitops, prompts, update
from release_cli.config import LOCAL_NAME, TEAM_NAME, Config, ConfigError, load, load_layers, set_connector_value, update_file
from release_cli.ui import fail, info, warn
from release_cli.versioning import PlanError, parse

COMMANDS = ("connector", "connectors", "hook", "edit", "update")
GITHUB_URL = re.compile(r"^(https://[^/@\s]+/[^/@\s]+/[^/@\s]+?)(?:\.git)?/?(?:@(\S+))?$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SLACK_WARNING = (
    "slack_cloud sends the review to a Cursor Cloud Agent instead of the local `agent`. "
    "It runs in the cloud: that agent needs the same integrations (e.g. the Datadog MCP) to see the errors, "
    "and cloud_command must be set to the command that starts it."
)
DISCOVER = """\
import json
from importlib.metadata import entry_points
out = []
for ep in entry_points(group="release.connectors"):
    url = ""
    try:
        url = json.loads(ep.dist.read_text("direct_url.json") or "{}").get("url", "")
    except Exception:
        pass
    out.append({"name": ep.name, "dist": ep.dist.name if ep.dist else "", "url": url})
print(json.dumps(out))
"""


def _defaults_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--defaults", action="store_true", help="take every default without asking")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release")
    sub = parser.add_subparsers(dest="command", required=True)

    connector = sub.add_parser("connector", help="install and manage connectors (global, ~/.config/release/connectors.toml)")
    csub = connector.add_subparsers(dest="action", required=True)
    add = csub.add_parser("add", help="install from a GitHub URL (pin with @ref) or add the built-in cursor-review")
    add.add_argument("target", help="https://github.com/org/repo[@ref] or cursor-review")
    _defaults_flag(add)
    csub.add_parser("ls", help="list installed connectors, order, and installed SHA")
    update = csub.add_parser("update", help="reinstall a connector from its source (explicit; never during a release)")
    update.add_argument("name")
    _defaults_flag(update)
    remove = csub.add_parser("remove", help="uninstall a connector")
    remove.add_argument("name")

    chain = sub.add_parser("connectors", help="run or inspect the post-push chain of a tag already on the remote")
    chsub = chain.add_subparsers(dest="action", required=True)
    run = chsub.add_parser("run", help="run the chain again for <tag>")
    run.add_argument("tag")
    pick = run.add_mutually_exclusive_group()
    pick.add_argument("--from", dest="start", metavar="NAME", help="start at NAME; earlier connectors feed their saved results")
    pick.add_argument("--only", metavar="NAME", help="run only NAME, with every other saved result as prior")
    run.add_argument("--dry-run", action="store_true", help="show questions; never call run")
    _defaults_flag(run)
    status = chsub.add_parser("status", help="show the saved state and review jobs of <tag>")
    status.add_argument("tag")

    hook = sub.add_parser("hook", help="add a pre-push hook (shell command) to this project")
    hsub = hook.add_subparsers(dest="action", required=True)
    hadd = hsub.add_parser("add")
    hadd.add_argument("--when", choices=("before", "after"), required=True)
    src = hadd.add_mutually_exclusive_group(required=True)
    src.add_argument("--cmd", help="shell command")
    src.add_argument("--url", help="https URL of a script to download and run")
    hadd.add_argument("--default", choices=("y", "n"), default="y", help="answer taken on Enter or with --defaults")
    hadd.add_argument("--team", action="store_true", help=f"write to {TEAM_NAME} instead of {LOCAL_NAME}")

    upd = sub.add_parser(
        "update",
        help="pull the release-cli clone and reinstall it (from any directory); asks only new config keys",
        description="Pull the release-cli clone (source.toml or the uv receipt), reinstall with uv, ask only new or "
        "changed global config keys. Never updates connectors, connectors.toml, or project .release files.",
    )
    _defaults_flag(upd)
    upd.add_argument("--config-only", action="store_true", help=argparse.SUPPRESS)
    upd.add_argument("--after-install", metavar="CLONE", help=argparse.SUPPRESS)

    edit = sub.add_parser("edit", help="edit hooks, connectors, order, and connector config (interactive without args)")
    edit.add_argument("words", nargs=argparse.REMAINDER, help="one editor command, e.g. `config set platform-deploy jira_project_key FRAUD`")
    return parser


def main(argv: list[str]) -> None:
    ns = build_parser().parse_args(argv)
    try:
        if ns.command == "connector":
            {"add": lambda: connector_add(ns.target, defaults=ns.defaults), "ls": connector_ls,
             "update": lambda: connector_update(ns.name, defaults=ns.defaults), "remove": lambda: connector_remove(ns.name)}[ns.action]()
        elif ns.command == "connectors":
            if ns.action == "run":
                connectors_run(ns.tag, start=ns.start, only=ns.only, defaults=ns.defaults, dry_run=ns.dry_run)
            else:
                connectors_status(ns.tag)
        elif ns.command == "update":
            update_command(ns)
        elif ns.command == "hook":
            hook_add(Path.cwd(), when=ns.when, cmd=ns.cmd, url=ns.url, default=ns.default == "y", team=ns.team)
        else:
            edit(Path.cwd(), ns.words)
    except (cx.ConnectorError, ConfigError, gitops.GitError, prompts.PromptError, update.UpdateError) as exc:
        fail(str(exc))


# ---------- update ----------


def update_command(ns: argparse.Namespace) -> None:
    if ns.after_install:
        after_install(Path(ns.after_install), defaults=ns.defaults or not sys.stdin.isatty())
    elif ns.config_only:
        asked = update.ensure_config(defaults=ns.defaults, log=info)
        info(f"config saved: {', '.join(asked)}" if asked else "config: nothing new to ask")
    else:
        update.update(defaults=ns.defaults, log=info)


def after_install(root: Path, *, defaults: bool, ask: prompts.Ask = input) -> None:
    """install.sh: remember the clone; offer cursor-review only on first install (notify unset); then new keys."""
    if update.register_source(root) is None:
        warn(f"{root} is not a git clone; `release update` will not find it (reinstall from {update.INSTALL_URL})")
    data = cx.load_release_toml()
    if "notify" not in data.get("cursor-review", {}):
        enable = prompts.ask_bool(
            "Enable cursor-review (local Cursor agent looks at errors 10 min after a deploy, desktop notification)?",
            False,
            defaults=defaults,
            ask=ask,
        )
        data.setdefault("cursor-review", {})["notify"] = enable
        cx.save_release_toml(data)
        if enable and "cursor-review" not in cx.load_global()["connectors"]:
            connector_add("cursor-review", defaults=True)
        elif not enable:
            info("cursor-review off. Enable later: release connector add cursor-review")
    update.ensure_config(defaults=defaults, ask=ask, log=info)


# ---------- connector install ----------


def split_target(target: str) -> tuple[str, str]:
    match = GITHUB_URL.match(target.strip())
    if not match:
        raise cx.ConnectorError(f"expected https://github.com/org/repo[@ref], got {target!r}")
    return match.group(1), match.group(2) or ""


def _norm_url(url: str) -> str:
    url = url.strip().lower().removeprefix("git+").rstrip("/")
    return url[:-4] if url.endswith(".git") else url


def _run(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise cx.ConnectorError(f"{' '.join(args[:3])} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def resolve_sha(url: str, ref: str) -> str:
    if SHA_RE.match(ref):
        return ref
    lines = [line.split("\t") for line in _run(["git", "ls-remote", url, ref or "HEAD"]).splitlines() if "\t" in line]
    if not lines:
        raise cx.ConnectorError(f"ref {ref or 'HEAD'!r} not found in {url}")
    for suffix in ("^{}", ""):
        for sha, name in lines:
            if name in (f"refs/tags/{ref}{suffix}",) or (not suffix and name in (f"refs/heads/{ref}", "HEAD")):
                return sha
    return lines[0][0]


def install(url: str, sha: str) -> list[dict[str, str]]:
    uv = shutil.which("uv")
    if uv is None:
        raise cx.ConnectorError("uv is required: curl -LsSf https://astral.sh/uv/install.sh | sh")
    if not cx.env_python().is_file():
        _run([uv, "venv", str(cx.env_dir())])
    _run([uv, "pip", "install", "--python", str(cx.env_python()), "--reinstall", f"git+{url}@{sha}"])
    found = json.loads(_run([str(cx.env_python()), "-c", DISCOVER]))
    return [ep for ep in found if _norm_url(ep["url"]) == _norm_url(url)]


def _ask_position(name: str, size: int, *, defaults: bool, ask: prompts.Ask) -> int:
    while True:
        raw = prompts.ask_text(f"Position of {name} in the order (1-{size + 1})", str(size + 1), defaults=defaults, ask=ask)
        if raw.isdigit() and 1 <= int(raw) <= size + 1:
            return int(raw) - 1


def register(
    g: dict[str, Any],
    names: list[str],
    base: dict[str, Any],
    *,
    break_default: bool,
    defaults: bool,
    ask: prompts.Ask = input,
) -> list[str]:
    """Add or refresh connectors in the global config. Existing ones keep their position and flags."""
    added: list[str] = []
    for name in names:
        cx.check_name(name)
        entry = g["connectors"].get(name)
        if entry is not None:
            if entry.get("source", "") != base.get("source", ""):
                raise cx.ConnectorError(f"{name} is already installed from {entry.get('source') or 'built-in'}; remove it first")
            entry.update(base)
            continue
        pos = _ask_position(name, len(g["order"]), defaults=defaults, ask=ask)
        boe = prompts.ask_bool(f"break_on_error for {name} (stop the chain when it fails)?", break_default, defaults=defaults, ask=ask)
        enabled = prompts.ask_bool(f"Enable {name}?", True, defaults=defaults, ask=ask)
        g["connectors"][name] = {**base, "enabled": enabled, "break_on_error": boe}
        g["order"] = [n for n in g["order"] if n != name]
        g["order"].insert(pos, name)
        added.append(name)
    return added


def connector_add(target: str, *, defaults: bool, ask: prompts.Ask = input) -> None:
    g = cx.load_global()
    if target in cx.BUILTINS:
        base: dict[str, Any] = {"builtin": True}
        if target not in g["connectors"]:
            base["config"] = {"slack_cloud": False, "delay_minutes": 10}
        register(g, [target], base, break_default=False, defaults=defaults, ask=ask)
        cx.save_global(g)
        info(f"{target}: built in, order {g['order'].index(target) + 1}")
        return
    url, ref = split_target(target)
    sha = resolve_sha(url, ref)
    info(f"installing {url}@{sha[:12]} into {cx.env_dir()}")
    eps = install(url, sha)
    if not eps:
        raise cx.ConnectorError(f"{url} installs no `release.connectors` entry points")
    base = {"source": url, "ref": ref, "sha": sha, "dist": eps[0]["dist"]}
    names = [ep["name"] for ep in eps]
    added = register(g, names, base, break_default=True, defaults=defaults, ask=ask)
    for name in names:
        g["connectors"][name]["entry_point"] = name
    cx.save_global(g)
    for name in names:
        state = "added" if name in added else "updated (order kept)"
        info(f"{name}: {state}, sha {sha[:12]}, order {g['order'].index(name) + 1}")


def connector_update(name: str, *, defaults: bool) -> None:
    entry = cx.load_global()["connectors"].get(name)
    if entry is None:
        raise cx.ConnectorError(f"unknown connector {name!r}")
    if entry.get("builtin") or not entry.get("source"):
        info(f"{name} is not installed from a URL; nothing to update")
        return
    connector_add(entry["source"] + (f"@{entry['ref']}" if entry.get("ref") else ""), defaults=defaults)


def connector_remove(name: str) -> None:
    g = cx.load_global()
    entry = g["connectors"].pop(name, None)
    if entry is None:
        raise cx.ConnectorError(f"unknown connector {name!r}")
    g["order"] = [n for n in g["order"] if n != name]
    cx.save_global(g)
    dist = entry.get("dist")
    still_used = any(e.get("dist") == dist for e in g["connectors"].values())
    uv = shutil.which("uv")
    if dist and not still_used and uv and cx.env_python().is_file():
        subprocess.run([uv, "pip", "uninstall", "--python", str(cx.env_python()), dist], capture_output=True, check=False)
    info(f"removed {name}")


def connector_ls() -> None:
    g = cx.load_global()
    cfg = _project_or_none(Path.cwd())
    conns = cx.resolve(g, cfg.connectors if cfg else {}, warn=warn)
    if not conns:
        info("no connectors installed. add one: release connector add https://github.com/org/repo")
        return
    for idx, conn in enumerate(conns, start=1):
        entry = g["connectors"][conn.name]
        where = "built-in" if entry.get("builtin") else entry.get("source", " ".join(conn.command))
        ref = f"@{entry['ref']}" if entry.get("ref") else ""
        sha = f" sha={entry['sha'][:12]}" if entry.get("sha") else ""
        info(
            f"{idx}. {conn.name}  enabled={'yes' if conn.enabled else 'no'} ({conn.origin['enabled']})  "
            f"break_on_error={'yes' if conn.break_on_error else 'no'}{sha}  {where}{ref}"
        )


# ---------- post-push chain ----------


def _project_or_none(cwd: Path) -> Config | None:
    try:
        return load(cwd)
    except ConfigError:
        return None


def _prior_from(saved: dict[str, Any], names: list[str]) -> dict[str, Any]:
    return {n: {k: saved[n].get(k) for k in ("status", "result", "error", "job_id")} for n in names if n in saved}


def post_push(
    cwd: Path,
    cfg: Config,
    release_version: str,
    sha: str,
    *,
    defaults: bool,
    dry_run: bool,
    start: str | None = None,
    only: str | None = None,
    ask: prompts.Ask = input,
) -> bool:
    """The post-push phase. Returns False when a connector failed. Never resets or deletes tags."""
    conns = cx.resolve(cx.load_global(), cfg.connectors, warn=warn)
    if not conns:
        info("CONNECTORS: none")
        return True
    context = cx.build_context(cwd, gitops.remote_url(), cfg.artifact, release_version, sha)
    state = cx.load_state(context["repo"], release_version) or cx.new_state(context)
    state.update({k: v for k, v in context.items() if k != "protocol"})
    saved = state.setdefault("connectors", {})
    names = [c.name for c in conns]
    for picked in (start, only):
        if picked is not None and picked not in names:
            raise cx.ConnectorError(f"unknown connector {picked!r}; installed: {', '.join(names)}")
    selected, prior = conns, {}
    if only:
        selected = [c for c in conns if c.name == only]
        selected[0].enabled = True
        prior = _prior_from(saved, [n for n in names if n != only])
    elif start:
        idx = names.index(start)
        selected = conns[idx:]
        prior = _prior_from(saved, names[:idx])
    return cx.run_chain(
        context, selected, state=state, prior=prior, cwd=cwd, project=dict(cfg.connectors),
        defaults=defaults, dry_run=dry_run, log=info, ask=ask,
    )


def release_version_of(tag: str, artifact: str) -> str:
    version = tag[len(artifact) + 1 :] if tag.startswith(f"{artifact}-") else tag
    try:
        parsed = parse(version)
    except PlanError as exc:
        raise cx.ConnectorError(f"{tag} is not a release tag of {artifact}") from exc
    if parsed.snapshot:
        raise cx.ConnectorError(f"{tag} is a SNAPSHOT, not a release tag")
    return version


def _require_project(cwd: Path) -> Config:
    cfg = load(cwd)
    if cfg is None:
        raise ConfigError("no .release or release.toml here; run `release --init`")
    return cfg


def connectors_run(tag: str, *, start: str | None, only: str | None, defaults: bool, dry_run: bool) -> None:
    cwd = Path.cwd()
    cfg = _require_project(cwd)
    version = release_version_of(tag, cfg.artifact)
    gitops.current_repo()
    if not gitops.remote_tag_exists(tag):
        raise cx.ConnectorError(f"tag {tag} is not on origin; connectors only run for pushed tags")
    if not gitops.tag_exists_local(tag):
        gitops.fetch_tag(tag)
    try:
        ok = post_push(cwd, cfg, version, gitops.tag_commit(tag), defaults=defaults, dry_run=dry_run, start=start, only=only)
    except (cx.ChainInterrupted, KeyboardInterrupt):
        info(f"interrupted. the tag stays. see: release connectors status {version}")
        raise SystemExit(130)
    if not ok:
        fail(f"a connector did not finish for {version}; the tag stays. see: release connectors status {version}")


def connectors_status(tag: str) -> None:
    cwd = Path.cwd()
    cfg = _require_project(cwd)
    version = release_version_of(tag, cfg.artifact)
    repo = cx.repo_name(gitops.remote_url(), cwd)
    state = cx.load_state(repo, version)
    if state is None:
        info(f"no connector runs saved for {repo} {version}")
    else:
        info(f"{repo} {version} ({cx.state_path(repo, version)})")
        for name, entry in state.get("connectors", {}).items():
            job = f" job_id={entry['job_id']}" if entry.get("job_id") else ""
            err = f" - {entry['error']}" if entry.get("error") else ""
            info(f"  {name}: {entry['status']}{job} at {entry.get('updated_at', '?')}{err}")
            if entry["status"] != "ok":
                info(f"    resume: {cx.resume_hint(version, name)}")
    for job in cursor_review.list_jobs(repo, version):
        alive = "alive" if cursor_review.pid_alive(job.get("pid")) else "not running"
        info(f"  review job: {job['status']} target {job['target_at']} pid {job.get('pid')} ({alive}) output {job['output']}")


# ---------- hooks ----------


def _download_hook(url: str) -> str:
    if not url.startswith("https://"):
        raise ConfigError("hook --url must be https")
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (https enforced above)
        body = resp.read()
    digest = hashlib.sha256(body).hexdigest()
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url.rstrip("/").rsplit("/", 1)[-1]) or "hook"
    path = cx.data_dir() / "hooks" / f"{digest[:12]}-{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o755)
    info(f"downloaded {url} -> {path} (sha256 {digest}). it runs as a shell hook; review it before releasing")
    return shlex.quote(str(path))


def hook_add(cwd: Path, *, when: str, cmd: str | None, url: str | None, default: bool, team: bool) -> None:
    if url and team:
        raise ConfigError("--url hooks are downloaded to this machine; they cannot go in release.toml")
    command = _download_hook(url) if url else (cmd or "").strip()
    if not command:
        raise ConfigError("empty hook command")
    hook: dict[str, Any] = {"when": when, "cmd": command}
    if not default:
        hook["default"] = False
    update_file(cwd, TEAM_NAME if team else LOCAL_NAME, lambda data: data.setdefault("hooks", []).append(hook))
    info(f"hook added ({when}): {command}")


# ---------- edit ----------

EDIT_HELP = """\
commands (values: y/n for flags, anything else as text):
  show
  hook add before|after <cmd...>        hook rm <n>        hook move <n> <to>
  hook set <n> when|cmd|enabled|default <value>
  connector add <github-url>[@ref]|cursor-review         connector rm <name>
  connector move <name> <position> [--project]
  connector set <name> enabled|break_on_error <y|n> [--project]
  config set <name> <key> <value> [--team]    per-project value (.release, or release.toml with --team)
  config unset <name> <key> [--team]
  config share <name> <key>                   copy a .release value to release.toml
  global set <name> <key> <value>             connector config in ~/.config/release/connectors.toml
  help | quit"""


def _flag(value: str) -> bool:
    low = value.lower()
    if low in ("y", "yes", "true", "on"):
        return True
    if low in ("n", "no", "false", "off"):
        return False
    raise ConfigError(f"expected y/n, got {value!r}")


def _scalar(value: str) -> Any:
    return {"true": True, "false": False}.get(value.lower(), value)


def show(cwd: Path) -> None:
    team, local = load_layers(cwd)
    merged = load(cwd)
    hooks_src = LOCAL_NAME if local and local.hooks_explicit else TEAM_NAME
    info(f"HOOKS (pre-push, {hooks_src})")
    for idx, hook in enumerate(merged.hooks if merged else (), start=1):
        flags = f"{'on' if hook.enabled else 'off'}, default {'y' if hook.default else 'n'}"
        info(f"  {idx}. {hook.when:<6} [{flags}] {hook.cmd}")
    g = cx.load_global()
    conns = cx.resolve(g, merged.connectors if merged else {}, warn=warn)
    info(f"CONNECTORS (post-push, {cx.global_path()})")
    for idx, conn in enumerate(conns, start=1):
        info(
            f"  {idx}. {conn.name} (order: {conn.origin['order']})  enabled={'y' if conn.enabled else 'n'} ({conn.origin['enabled']})"
            f"  break_on_error={'y' if conn.break_on_error else 'n'} (global)"
        )
        registry = cx.load_release_toml().get(conn.name, {})
        values: dict[str, tuple[Any, str]] = {k: (v, "~/.config/release/release.toml") for k, v in registry.items()}
        values.update({k: (v, "connectors.toml") for k, v in g["connectors"][conn.name].get("config", {}).items()})
        for layer, label in ((team, TEAM_NAME), (local, LOCAL_NAME)):
            for k, v in (layer.connectors.get(conn.name, {}) if layer else {}).items():
                values[k] = (v, label)
        for key, (value, label) in values.items():
            info(f"       {key} = {json.dumps(value)} ({label})")


def _project_lists(cwd: Path, change) -> None:
    def apply(data: dict[str, Any]) -> None:
        change(data.setdefault("connectors", {}))

    update_file(cwd, LOCAL_NAME, apply)


def edit_command(cwd: Path, words: list[str], *, ask: prompts.Ask = input) -> None:
    project = "--project" in words
    team = "--team" in words
    words = [w for w in words if w not in ("--project", "--team")]
    head = words[:2]
    if not words or words == ["show"]:
        show(cwd)
    elif words == ["help"]:
        info(EDIT_HELP)
    elif head == ["hook", "add"] and len(words) >= 4 and words[2] in ("before", "after"):
        hook_add(cwd, when=words[2], cmd=" ".join(words[3:]), url=None, default=True, team=team)
    elif head == ["hook", "rm"] and len(words) == 3:
        _edit_hooks(cwd, team, lambda hooks: hooks.pop(_index(words[2], hooks)))
    elif head == ["hook", "move"] and len(words) == 4:
        _edit_hooks(cwd, team, lambda hooks: hooks.insert(_index(words[3], hooks + [None]), hooks.pop(_index(words[2], hooks))))
    elif head == ["hook", "set"] and len(words) >= 5 and words[3] in ("when", "cmd", "enabled", "default"):
        key, raw = words[3], " ".join(words[4:])
        value: Any = _flag(raw) if key in ("enabled", "default") else raw
        _edit_hooks(cwd, team, lambda hooks: hooks[_index(words[2], hooks)].__setitem__(key, value))
    elif head == ["connector", "add"] and len(words) == 3:
        connector_add(words[2], defaults=False, ask=ask)
    elif head == ["connector", "rm"] and len(words) == 3:
        connector_remove(words[2])
    elif head == ["connector", "move"] and len(words) == 4:
        _move_connector(cwd, words[2], words[3], project=project)
    elif head == ["connector", "set"] and len(words) == 5 and words[3] in ("enabled", "break_on_error"):
        _set_connector_flag(cwd, words[2], words[3], _flag(words[4]), project=project)
    elif head == ["config", "set"] and len(words) >= 5:
        set_connector_value(cwd, words[2], words[3], _scalar(" ".join(words[4:])), name=TEAM_NAME if team else LOCAL_NAME)
        info(f"{words[2]}.{words[3]} saved in {TEAM_NAME if team else LOCAL_NAME}")
    elif head == ["config", "unset"] and len(words) == 4:
        update_file(cwd, TEAM_NAME if team else LOCAL_NAME, lambda d: d.get("connectors", {}).get(words[2], {}).pop(words[3], None))
    elif head == ["config", "share"] and len(words) == 4:
        _, local = load_layers(cwd)
        values = local.connectors.get(words[2], {}) if local else {}
        if words[3] not in values:
            raise ConfigError(f"{words[2]}.{words[3]} is not set in {LOCAL_NAME}")
        set_connector_value(cwd, words[2], words[3], values[words[3]], name=TEAM_NAME)
        info(f"{words[2]}.{words[3]} copied to {TEAM_NAME}; commit it to share")
    elif head == ["global", "set"] and len(words) >= 5:
        _global_set(words[2], words[3], _scalar(" ".join(words[4:])))
    else:
        raise ConfigError(f"unknown edit command: {' '.join(words)}. try `help`")


def _index(raw: str, items: list[Any]) -> int:
    if not raw.isdigit() or not 1 <= int(raw) <= len(items):
        raise ConfigError(f"expected a number from 1 to {len(items)}, got {raw!r}")
    return int(raw) - 1


def _edit_hooks(cwd: Path, team: bool, change) -> None:
    update_file(cwd, TEAM_NAME if team else LOCAL_NAME, lambda data: change(data.setdefault("hooks", [])))


def _move_connector(cwd: Path, name: str, raw_pos: str, *, project: bool) -> None:
    g = cx.load_global()
    if name not in g["connectors"]:
        raise cx.ConnectorError(f"unknown connector {name!r}")
    if project:
        merged = load(cwd)
        order = [c.name for c in cx.resolve(g, merged.connectors if merged else {})]
    else:
        order = [c.name for c in cx.resolve(g, {})]
    order.remove(name)
    order.insert(_index(raw_pos, order + [name]), name)
    if project:
        _project_lists(cwd, lambda c: c.__setitem__("order", order))
    else:
        g["order"] = order
        cx.save_global(g)
    info(f"order ({'project' if project else 'global'}): {', '.join(order)}")


def _set_connector_flag(cwd: Path, name: str, key: str, value: bool, *, project: bool) -> None:
    g = cx.load_global()
    if name not in g["connectors"]:
        raise cx.ConnectorError(f"unknown connector {name!r}")
    if not project:
        g["connectors"][name][key] = value
        cx.save_global(g)
        return
    if key != "enabled":
        raise ConfigError("break_on_error is global; the project can override order, enabled, and disabled")

    def change(c: dict[str, Any]) -> None:
        for lst in ("enabled", "disabled"):
            c[lst] = [n for n in c.get(lst, []) if n != name]
        c["enabled" if value else "disabled"].append(name)

    _project_lists(cwd, change)


def _global_set(name: str, key: str, value: Any) -> None:
    if any(k.table == name and k.name == key for k in update.REGISTRY):
        data = cx.load_release_toml()
        data.setdefault(name, {})[key] = value
        cx.save_release_toml(data)
        info(f"{name}.{key} saved in {cx.release_toml_path()}")
        return
    g = cx.load_global()
    if name not in g["connectors"]:
        raise cx.ConnectorError(f"unknown connector {name!r}")
    if key == "timeout_seconds":
        g["connectors"][name][key] = float(value)
    else:
        g["connectors"][name].setdefault("config", {})[key] = value
    cx.save_global(g)
    if name == "cursor-review" and key == "slack_cloud" and value is True:
        warn(SLACK_WARNING)


def edit(cwd: Path, words: list[str], *, ask: prompts.Ask = input) -> None:
    if words:
        edit_command(cwd, words, ask=ask)
        return
    show(cwd)
    info("type `help` for commands, `quit` to leave")
    while True:
        try:
            line = ask("edit> ").strip()
        except EOFError:
            return
        if line in ("q", "quit", "exit"):
            return
        if not line:
            continue
        try:
            edit_command(cwd, shlex.split(line), ask=ask)
        except (ConfigError, cx.ConnectorError) as exc:
            print(f"error: {exc}", file=sys.stderr)
