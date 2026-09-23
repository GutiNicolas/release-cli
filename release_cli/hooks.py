"""Run user-configured release hooks: shell commands, before the atomic push."""

from __future__ import annotations

import subprocess

from release_cli import prompts
from release_cli.config import Hook, When


class HookError(RuntimeError):
    pass


def run_command(cmd: str) -> int:
    return subprocess.run(cmd, shell=True, check=False).returncode


def question(hook: Hook) -> str:
    if hook.when == "before":
        return f"Would you like to run [{hook.cmd}] before releasing?"
    return f"Would you like to run [{hook.cmd}] after setting version?"


def run_hooks(
    hooks: tuple[Hook, ...],
    when: When,
    *,
    defaults: bool,
    skip: bool,
    dry_run: bool,
    log,
    ask=input,
) -> None:
    for hook in hooks:
        if hook.when != when or not hook.enabled:
            continue
        if dry_run:
            log(f"HOOK {when}: {hook.cmd} [{'y' if hook.default else 'n'}]")
            continue
        if skip or not prompts.ask_bool(question(hook), hook.default, defaults=defaults, ask=ask):
            log(f"skipping [{hook.cmd}]")
            continue
        log(f"running [{hook.cmd}]")
        code = run_command(hook.cmd)
        if code != 0:
            raise HookError(f"command failed ({code}): {hook.cmd}")
