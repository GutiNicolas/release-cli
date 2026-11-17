"""Console output shared by the release flow and the subcommands."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, NoReturn, TextIO

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
NC = "\033[0m"


def _color(stream: TextIO) -> bool:
    return stream.isatty() and not os.environ.get("NO_COLOR")


def _paint(color: str, text: str, stream: TextIO | None = None) -> str:
    """Colour only when the stream the text goes to is a terminal (stdout unless told otherwise)."""
    if not _color(stream or sys.stdout):
        return text
    return f"{color}{text}{NC}"


class _Mode:
    quiet = False
    json = False
    json_out: TextIO | None = None  # the real stdout while --json sends everything else to stderr


def set_mode(*, quiet: bool = False, as_json: bool = False) -> None:
    """--quiet: only the result line and errors. --json: stdout carries one JSON object; the rest goes to stderr."""
    _Mode.quiet, _Mode.json = quiet, as_json
    if as_json and _Mode.json_out is None:
        _Mode.json_out = sys.stdout
        sys.stdout = sys.stderr  # progress, prompts (input()), and hook output stay off the JSON stream


def reset_mode() -> None:
    if _Mode.json_out is not None:
        sys.stdout = _Mode.json_out
    _Mode.quiet, _Mode.json, _Mode.json_out = False, False, None


def is_json() -> bool:
    return _Mode.json


def child_stdout() -> TextIO | None:
    """Where hook commands write: inherited normally, stderr under --quiet/--json."""
    return sys.stderr if _Mode.quiet or _Mode.json else None


def info(msg: str) -> None:
    if _Mode.quiet:
        return
    # Flushed so a piped stdout (CI, `| tee`) keeps its place among connector stderr lines.
    print(msg, flush=True)


def alert(msg: str) -> None:
    """Like info, but --quiet sends it to stderr instead of dropping it (connector failures, resume hints)."""
    if _Mode.quiet:
        print(msg, file=sys.stderr, flush=True)
    else:
        print(msg, flush=True)


def result(msg: str) -> None:
    """The one line --quiet keeps (nothing under --json: the JSON object is the result)."""
    if not _Mode.json:
        print(msg, flush=True)


def emit(data: dict[str, Any]) -> None:
    """The --json result object. A no-op in every other mode."""
    if not _Mode.json:
        return
    out = _Mode.json_out or sys.stdout
    out.write(json.dumps(data) + "\n")
    out.flush()


def warn(msg: str) -> None:
    print(_paint(YELLOW, f"WARNING: {msg}", sys.stderr), file=sys.stderr)


def fail(msg: str, code: int = 1, **extra: Any) -> NoReturn:
    print(_paint(RED, f"ERROR: {msg}", sys.stderr), file=sys.stderr)
    emit({"ok": False, "error": msg, "exit_code": code, **extra})
    raise SystemExit(code)
