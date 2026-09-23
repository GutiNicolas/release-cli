from __future__ import annotations

import pytest

from release_cli import hooks
from release_cli.config import Hook
from release_cli.hooks import question, run_hooks


def test_question_uses_configured_command() -> None:
    before = Hook(when="before", cmd="mvn test")
    after = Hook(when="after", cmd="mvn deploy")
    assert "[mvn test]" in question(before)
    assert "before releasing" in question(before)
    assert "[mvn deploy]" in question(after)
    assert "after setting version" in question(after)


def _ran(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ran: list[str] = []
    monkeypatch.setattr(hooks, "run_command", lambda cmd: ran.append(cmd) or 0)
    return ran


def test_n_skips_and_enter_takes_hook_default(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _ran(monkeypatch)
    all_hooks = (Hook("after", "mvn deploy"), Hook("after", "notify", default=False), Hook("after", "off", enabled=False))
    answers = iter(["n", ""])
    run_hooks(all_hooks, "after", defaults=False, skip=False, dry_run=False, log=lambda _m: None, ask=lambda _p: next(answers))
    assert ran == []


def test_defaults_takes_configured_default_not_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _ran(monkeypatch)
    all_hooks = (Hook("before", "mvn test"), Hook("before", "slow", default=False))
    run_hooks(all_hooks, "before", defaults=True, skip=False, dry_run=False, log=lambda _m: None, ask=lambda _p: pytest.fail("asked"))
    assert ran == ["mvn test"]


def test_skip_never_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _ran(monkeypatch)
    run_hooks((Hook("before", "mvn test"),), "before", defaults=False, skip=True, dry_run=False, log=lambda _m: None, ask=lambda _p: pytest.fail("asked"))
    assert ran == []


def test_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "run_command", lambda _cmd: 3)
    with pytest.raises(hooks.HookError, match="failed \\(3\\)"):
        run_hooks((Hook("before", "false"),), "before", defaults=True, skip=False, dry_run=False, log=lambda _m: None)
