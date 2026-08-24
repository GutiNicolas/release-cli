from __future__ import annotations

from release_cli.config import Hook
from release_cli.hooks import prompt_text, want_hook


def test_prompt_uses_configured_command() -> None:
    before = Hook(when="before", cmd="mvn test")
    after = Hook(when="after", cmd="mvn deploy")
    assert "[mvn test]" in prompt_text(before)
    assert "before releasing" in prompt_text(before)
    assert "[mvn deploy]" in prompt_text(after)
    assert "after setting version" in prompt_text(after)


def test_want_hook_n_skips() -> None:
    hook = Hook(when="after", cmd="mvn deploy")
    assert want_hook(hook, yes=False, skip=False, dry_run=False, ask=lambda _p: "n") is False
    assert want_hook(hook, yes=True, skip=False, dry_run=False, ask=lambda _p: "n") is True
    assert want_hook(hook, yes=False, skip=True, dry_run=False, ask=lambda _p: "y") is False
