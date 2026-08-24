"""Interactive first-run install: .release, optional release.toml, gitignore."""

from __future__ import annotations

import sys
from pathlib import Path

from release_cli import adapters
from release_cli.adapters.base import AdapterError
from release_cli.config import (
    LOCAL_NAME,
    TEAM_NAME,
    Config,
    Hook,
    When,
    load,
    write_local,
    write_team,
)
from release_cli.detect import describe_unsupported, detect


class InitError(RuntimeError):
    pass


class Prompter:
    def confirm(self, question: str, default: bool) -> bool:
        raise NotImplementedError

    def ask(self, question: str, default: str = "") -> str:
        raise NotImplementedError

    def choose(self, question: str, options: list[str]) -> str:
        raise NotImplementedError


class StdPrompter(Prompter):
    def confirm(self, question: str, default: bool) -> bool:
        suffix = "Y/n" if default else "y/N"
        raw = input(f"{question} ({suffix}): ").strip().lower()
        if raw == "":
            return default
        return raw.startswith("y")

    def ask(self, question: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        raw = input(f"{question}{hint}: ")
        if raw.strip() == "" and default:
            return default
        return raw.strip()

    def choose(self, question: str, options: list[str]) -> str:
        listed = ", ".join(options)
        while True:
            raw = self.ask(f"{question} ({listed})", options[0])
            if raw in options:
                return raw


class AutoPrompter(Prompter):
    """Non-interactive defaults: no hooks, no team file, yes gitignore."""

    def confirm(self, question: str, default: bool) -> bool:
        del question
        return default

    def ask(self, question: str, default: str = "") -> str:
        del question
        return default

    def choose(self, question: str, options: list[str]) -> str:
        del question
        return options[0]


def _ignores_release(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if stripped in {".release", "/.release", "**/.release"}:
            return True
    return False


def ensure_gitignore(cwd: Path, prompter: Prompter, log) -> bool:
    path = cwd / ".gitignore"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        if _ignores_release(text):
            return False
        if not prompter.confirm("Add .release to .gitignore?", True):
            log("WARNING: .release is personal and may be committed by mistake")
            return False
        prefix = "" if text.endswith("\n") or text == "" else "\n"
        path.write_text(text + f"{prefix}.release\n", encoding="utf-8")
        return True
    if not prompter.confirm("No .gitignore found. Create one containing .release?", True):
        log("WARNING: .release is personal and may be committed by mistake")
        return False
    path.write_text(".release\n", encoding="utf-8")
    return True


def _hints(tool: str, cwd: Path) -> tuple[str, str]:
    if tool == "maven":
        return "mvn test", "mvn deploy"
    if tool == "sbt":
        return "sbt test", "sbt publish"
    wrapper = (cwd / "gradlew").is_file() or (cwd / "gradlew.bat").is_file()
    exe = "./gradlew" if wrapper else "gradle"
    return f"{exe} test", f"{exe} publish"


def _collect_hooks(prompter: Prompter, tool: str, cwd: Path, log) -> tuple[Hook, ...]:
    before_hint, after_hint = _hints(tool, cwd)
    log(f"hint before: {before_hint}  |  hint after: {after_hint}")
    log("after = publish (run once the release version is written; before would publish SNAPSHOT)")
    hooks: list[Hook] = []
    add = prompter.confirm("Add a command to run during release?", False)
    while add:
        cmd = prompter.ask("Command")
        if not cmd:
            log("empty command skipped")
        else:
            when_raw = prompter.ask("When? before / after", "before").lower()
            when: When = "after" if when_raw.startswith("a") else "before"
            hooks.append(Hook(when=when, cmd=cmd))
        add = prompter.confirm("Add another?", False)
    return tuple(hooks)


def initialize(
    cwd: Path,
    *,
    tool: str | None = None,
    force: bool = False,
    prompter: Prompter | None = None,
    yes: bool = False,
    log=print,
) -> tuple[Config, bool]:
    """Returns (config, dirtied_git) where dirtied_git means gitignore/toml changed."""
    existing = load(cwd)
    if existing is not None and not force:
        raise InitError(f"already initialized ({LOCAL_NAME} or {TEAM_NAME} present). use --init --force")
    if prompter is None:
        prompter = AutoPrompter() if yes or not sys.stdin.isatty() else StdPrompter()

    found = detect(cwd)
    if tool:
        if tool not in found and found:
            log(f"WARNING: --tool {tool} but detected {', '.join(found)}")
        chosen = tool
    elif len(found) == 1:
        chosen = found[0]
        if not prompter.confirm(f"Detected {chosen}. Use it?", True):
            raise InitError("init cancelled")
    elif len(found) > 1:
        chosen = prompter.choose("Multiple build tools found. Which one?", found)
    else:
        raise InitError(describe_unsupported(cwd))

    adapter = adapters.get(chosen)  # type: ignore[arg-type]
    try:
        state = adapter.discover(cwd)
    except AdapterError as exc:
        raise InitError(str(exc)) from exc

    artifact = prompter.ask("Artifact name", state.artifact) or state.artifact
    hooks = _collect_hooks(prompter, chosen, cwd, log)
    cfg = Config(tool=chosen, artifact=artifact, version_file=state.version_file, hooks=hooks)  # type: ignore[arg-type]
    share = prompter.confirm("Share this config with the team (write release.toml)?", False)
    write_local(cwd, cfg)
    dirtied = False
    if share:
        write_team(cwd, cfg)
        dirtied = True
        log(f"wrote {TEAM_NAME}")
    log(f"wrote {LOCAL_NAME} (tool={cfg.tool}, artifact={cfg.artifact})")
    if ensure_gitignore(cwd, prompter, log):
        dirtied = True
        log("updated .gitignore")
    return cfg, dirtied
