"""Console output shared by the release flow and the subcommands."""

from __future__ import annotations

import os
import sys

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
NC = "\033[0m"


def _color() -> bool:
    return sys.stderr.isatty() and not os.environ.get("NO_COLOR")


def _paint(color: str, text: str) -> str:
    if not _color():
        return text
    return f"{color}{text}{NC}"


def info(msg: str) -> None:
    # Flushed so a piped stdout (CI, `| tee`) keeps its place among connector stderr lines.
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(_paint(YELLOW, f"WARNING: {msg}"), file=sys.stderr)


def fail(msg: str, code: int = 1) -> None:
    print(_paint(RED, f"ERROR: {msg}"), file=sys.stderr)
    raise SystemExit(code)
