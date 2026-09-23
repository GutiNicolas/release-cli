"""The one prompt module for init, hooks, and connectors: bool, choice, text.

`defaults=True` (the `--defaults` flag) takes the configured default without asking.
It never invents a value: a text question with no default raises PromptError.
"""

from __future__ import annotations

from typing import Callable

Ask = Callable[[str], str]


class PromptError(RuntimeError):
    pass


def ask_bool(prompt: str, default: bool, *, defaults: bool = False, ask: Ask = input) -> bool:
    if defaults:
        return default
    hint = "y" if default else "n"
    while True:
        raw = ask(f"{prompt} (y/n) [{hint}]: ").strip().lower()
        if raw == "":
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def ask_choice(prompt: str, options: list[str], default: str, *, defaults: bool = False, ask: Ask = input) -> str:
    if default not in options:
        raise PromptError(f"default {default!r} is not one of {options}")
    if defaults:
        return default
    listed = "/".join(options)
    while True:
        raw = ask(f"{prompt} ({listed}) [{default}]: ").strip()
        if raw == "":
            return default
        if raw in options:
            return raw


def ask_text(
    prompt: str,
    default: str | None = None,
    *,
    required: bool = True,
    defaults: bool = False,
    ask: Ask = input,
) -> str:
    if defaults:
        if default is not None:
            return default
        if not required:
            return ""
        raise PromptError(f"no value for: {prompt}")
    hint = f" [{default}]" if default else ""
    while True:
        raw = ask(f"{prompt}{hint}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        if not required:
            return ""
