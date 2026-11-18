"""The one prompt module for init, hooks, and connectors: bool, choice, multi, text.

`defaults=True` (the `--defaults` flag) takes the configured default without asking.
It never invents a value: a text question with no default raises PromptError.
"""

from __future__ import annotations

import re
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


def ask_multi(
    prompt: str,
    options: list[str],
    labels: list[str],
    default: list[str],
    *,
    other: str | None = None,
    defaults: bool = False,
    ask: Ask = input,
) -> list[str]:
    """A ticked list. Numbers toggle, `+KEY` adds (an option, or an extra matching `other`), `-KEY` removes, Enter accepts.

    The answer is the ticked options in option order, then the extras in the order they were added.
    """
    ticked = {v for v in default if v in options}
    extras = [v for v in default if v not in options]
    if defaults:
        return [v for v in options if v in ticked] + extras
    hint = "Enter accepts, numbers toggle, +KEY adds, -KEY removes" if other else "Enter accepts, numbers toggle"
    while True:
        print(prompt)
        rows = [(v, label, v in ticked) for v, label in zip(options, labels, strict=True)] + [(v, v, True) for v in extras]
        for n, (_v, label, on) in enumerate(rows, 1):
            print(f"  {n:>2}. [{'x' if on else ' '}] {label}")
        raw = ask(f"({hint}): ").strip()
        if not raw:
            return [v for v in options if v in ticked] + extras
        for token in raw.replace(",", " ").split():
            err = _apply_multi_token(token, rows, options, ticked, extras, other)
            if err:
                print(f"  {err}")


def _apply_multi_token(
    token: str, rows: list[tuple[str, str, bool]], options: list[str], ticked: set[str], extras: list[str], other: str | None
) -> str | None:
    """Apply one toggle/add/remove to `ticked`/`extras`; the error message when the token is not valid."""
    if token.isdigit():
        n = int(token)
        if not 1 <= n <= len(rows):
            return f"no item {token}"
        value = rows[n - 1][0]
        if value in extras:
            extras.remove(value)
        else:
            ticked.symmetric_difference_update({value})
        return None
    sign, key = token[:1], token[1:]
    if sign not in "+-" or not key:
        return f"{token!r}: use a number, +KEY or -KEY"
    if sign == "+":
        if key in options:
            ticked.add(key)
        elif key in extras:
            pass
        elif other is None or not re.fullmatch(other, key):
            return f"{key!r} is not in the list" + ("" if other is None else f" and does not match {other}")
        else:
            extras.append(key)
        return None
    if key in options:
        ticked.discard(key)
    elif key in extras:
        extras.remove(key)
    else:
        return f"{key!r} is not in the list"
    return None


def ask_text(
    prompt: str,
    default: str | None = None,
    *,
    required: bool = True,
    defaults: bool = False,
    ask: Ask = input,
    validate: Callable[[str], str | None] = lambda _v: None,
) -> str:
    """`validate` returns an error message or None. Invalid input is asked again; under defaults it raises."""
    if defaults:
        if default is None and not required:
            return ""
        if default is None:
            raise PromptError(f"no value for: {prompt}")
        if (err := validate(default)) is not None:
            raise PromptError(f"default {default!r} for {prompt!r} is invalid: {err}")
        return default
    hint = f" [{default}]" if default else ""
    while True:
        raw = ask(f"{prompt}{hint}: ").strip()
        if not raw:
            if default is not None:
                raw = default
            elif not required:
                return ""
            else:
                continue
        err = validate(raw)
        if err is None:
            return raw
        print(f"  {err}")


def int_error(raw: str, lo: int | None, hi: int | None) -> str | None:
    try:
        value = int(raw)
    except ValueError:
        return "enter a whole number"
    if lo is not None and value < lo:
        return f"must be >= {lo}"
    if hi is not None and value > hi:
        return f"must be <= {hi}"
    return None

