"""Linux and macOS: no Darwin-only call outside the guarded notification in cursor_review."""

from __future__ import annotations

import inspect
import re
import subprocess
from pathlib import Path

import pytest

from release_cli import cursor_review as cr

ROOT = Path(__file__).resolve().parents[1]
DARWIN_ONLY = re.compile(r"osascript|pbcopy|launchctl|/Users/|~/Library|/Library/|\bimport (AppKit|Foundation|objc)\b|sed -i ''")


def test_darwin_only_markers_only_in_guarded_notification() -> None:
    hits = []
    for path in [*sorted((ROOT / "release_cli").rglob("*.py")), ROOT / "install.sh"]:
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if DARWIN_ONLY.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{num}")
    guarded = {
        f"release_cli/cursor_review.py:{n}"
        for n in _lines_of(cr.notification_command) | _lines_of(cr.deeplink_open_command) | _lines_of(cr.focus_repo_command) | _lines_of_docstring()
    }
    assert set(hits) <= guarded, f"Darwin-only call outside notification_command: {sorted(set(hits) - guarded)}"


def _lines_of(fn) -> set[int]:
    lines, start = inspect.getsourcelines(fn)
    return set(range(start, start + len(lines)))


def _lines_of_docstring() -> set[int]:
    text = (ROOT / "release_cli" / "cursor_review.py").read_text(encoding="utf-8").splitlines()
    return {i for i, line in enumerate(text[:12], start=1) if "osascript" in line}


@pytest.mark.parametrize(("platform", "tool"), [("darwin", "osascript"), ("linux", "notify-send"), ("linux2", "notify-send")])
def test_notification_command_is_chosen_by_platform(monkeypatch: pytest.MonkeyPatch, platform: str, tool: str) -> None:
    monkeypatch.setattr(cr, "_platform", lambda: platform)
    monkeypatch.setattr(cr.shutil, "which", lambda name: f"/usr/bin/{name}")
    cmd = cr.notification_command("t", "m")
    assert cmd is not None and cmd[0] == tool


def test_install_sh_is_posix_sh() -> None:
    subprocess.run(["sh", "-n", str(ROOT / "install.sh")], check=True)
