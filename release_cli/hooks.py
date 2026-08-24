"""Run user-configured release hooks."""

from __future__ import annotations

import subprocess

from release_cli.config import Hook, When


class HookError(RuntimeError):
    pass


def run_command(cmd: str) -> int:
    return subprocess.run(cmd, shell=True, check=False).returncode


def prompt_text(hook: Hook) -> str:
    if hook.when == "before":
        return f"Would you like to run [{hook.cmd}] before releasing? (y/n) [y]: "
    return f"Would you like to run [{hook.cmd}] after setting version? (y/n) [y]: "


def want_hook(hook: Hook, *, yes: bool, skip: bool, dry_run: bool, ask) -> bool:
    if skip or dry_run:
        return False
    if yes:
        return True
    answer = ask(prompt_text(hook))
    return answer.strip() == "" or answer.strip().lower().startswith("y")


def run_hooks(
    hooks: tuple[Hook, ...],
    when: When,
    *,
    yes: bool,
    skip: bool,
    dry_run: bool,
    log,
    ask,
) -> None:
    for hook in hooks:
        if hook.when != when:
            continue
        if dry_run:
            log(f"HOOK {when}: {hook.cmd}")
            continue
        if not want_hook(hook, yes=yes, skip=skip, dry_run=False, ask=ask):
            log(f"skipping [{hook.cmd}]")
            continue
        log(f"running [{hook.cmd}]")
        code = run_command(hook.cmd)
        if code != 0:
            raise HookError(f"command failed ({code}): {hook.cmd}")
