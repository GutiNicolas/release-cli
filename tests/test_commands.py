from __future__ import annotations

import sys
from pathlib import Path

import pytest

from release_cli import commands, connectors
from release_cli.config import load
from tests.conftest import write_config


def test_register_asks_order_and_break_on_error_and_never_reorders() -> None:
    g = {"order": ["cursor-review"], "connectors": {"cursor-review": {"builtin": True}}}
    replies = iter(["1", "", "", "9", "2", "n", "y"])
    base = {"source": "https://github.com/koinlatam/fraud-platform-api-py-connector", "sha": "a" * 40}
    added = commands.register(g, ["platform-build", "platform-deploy"], base, break_default=True, defaults=False, ask=lambda _p: next(replies))
    assert added == ["platform-build", "platform-deploy"]
    assert g["order"] == ["platform-build", "platform-deploy", "cursor-review"]
    assert g["connectors"]["platform-build"]["break_on_error"] is True
    assert g["connectors"]["platform-deploy"]["break_on_error"] is False
    g["order"] = ["cursor-review", "platform-deploy", "platform-build"]
    again = commands.register(g, ["platform-build", "platform-deploy"], {**base, "sha": "b" * 40}, break_default=True, defaults=True, ask=lambda _p: pytest.fail("asked"))
    assert again == []
    assert g["order"] == ["cursor-review", "platform-deploy", "platform-build"]
    assert g["connectors"]["platform-build"]["sha"] == "b" * 40
    assert g["connectors"]["platform-deploy"]["break_on_error"] is False


def test_register_refuses_same_name_from_another_source() -> None:
    g = {"order": ["x"], "connectors": {"x": {"source": "https://github.com/a/one"}}}
    with pytest.raises(connectors.ConnectorError, match="already installed"):
        commands.register(g, ["x"], {"source": "https://github.com/a/two"}, break_default=True, defaults=True)


@pytest.mark.parametrize(
    ("target", "url", "ref"),
    [
        ("https://github.com/koinlatam/fraud-platform-api-py-connector", "https://github.com/koinlatam/fraud-platform-api-py-connector", ""),
        ("https://github.com/koinlatam/fraud-platform-api-py-connector.git@v0.1.0", "https://github.com/koinlatam/fraud-platform-api-py-connector", "v0.1.0"),
    ],
)
def test_split_target(target: str, url: str, ref: str) -> None:
    assert commands.split_target(target) == (url, ref)


def test_split_target_rejects_non_https() -> None:
    with pytest.raises(connectors.ConnectorError):
        commands.split_target("git@github.com:koinlatam/x.git")


def test_add_builtin_cursor_review_defaults(capsys: pytest.CaptureFixture[str]) -> None:
    commands.connector_add("cursor-review", defaults=True)
    entry = connectors.load_global()["connectors"]["cursor-review"]
    assert entry["builtin"] is True and entry["break_on_error"] is False
    assert entry["config"] == {"notify_macos": True, "slack_cloud": False, "delay_minutes": 10}
    [conn] = connectors.resolve(connectors.load_global(), {})
    assert conn.command == [sys.executable, "-m", "release_cli.cursor_review"]
    commands.connector_ls()
    assert "cursor-review  enabled=yes (global)  break_on_error=no" in capsys.readouterr().out


def test_hook_add_and_edit_hooks(tmp_path: Path) -> None:
    write_config(tmp_path)
    commands.hook_add(tmp_path, when="before", cmd="mvn test", url=None, default=True, team=False)
    commands.hook_add(tmp_path, when="after", cmd="mvn deploy", url=None, default=False, team=False)
    cfg = load(tmp_path)
    assert [(h.when, h.cmd, h.default) for h in cfg.hooks] == [("before", "mvn test", True), ("after", "mvn deploy", False)]
    commands.edit(tmp_path, ["hook", "move", "2", "1"])
    commands.edit(tmp_path, ["hook", "set", "2", "enabled", "n"])
    commands.edit(tmp_path, ["hook", "set", "1", "cmd", "mvn -B deploy"])
    cfg = load(tmp_path)
    assert [(h.cmd, h.enabled) for h in cfg.hooks] == [("mvn -B deploy", True), ("mvn test", False)]
    commands.edit(tmp_path, ["hook", "rm", "1"])
    assert [h.cmd for h in load(tmp_path).hooks] == ["mvn test"]


def test_hook_url_cannot_go_to_team_file(tmp_path: Path) -> None:
    write_config(tmp_path)
    with pytest.raises(commands.ConfigError):
        commands.hook_add(tmp_path, when="before", cmd=None, url="https://example.com/x.sh", default=True, team=True)


def test_edit_config_share_and_origins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_config(tmp_path)
    commands.connector_add("cursor-review", defaults=True)
    commands.edit(tmp_path, ["config", "set", "cursor-review", "extra_prompt", "Use", "the", "Datadog", "MCP"])
    commands.edit(tmp_path, ["config", "share", "cursor-review", "extra_prompt"])
    assert "Datadog" in (tmp_path / "release.toml").read_text(encoding="utf-8")
    commands.edit(tmp_path, ["show"])
    out = capsys.readouterr().out
    assert 'extra_prompt = "Use the Datadog MCP" (.release)' in out
    assert "notify_macos = true (connectors.toml)" in out


def test_edit_slack_cloud_warns(capsys: pytest.CaptureFixture[str]) -> None:
    commands.connector_add("cursor-review", defaults=True)
    commands.edit(Path.cwd(), ["global", "set", "cursor-review", "slack_cloud", "true"])
    assert "runs in the cloud" in capsys.readouterr().err
    assert connectors.load_global()["connectors"]["cursor-review"]["config"]["slack_cloud"] is True


def test_edit_interactive_loop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_config(tmp_path)
    lines = iter(["hook add before mvn verify", "bogus", "quit"])
    commands.edit(tmp_path, [], ask=lambda _p: next(lines))
    assert [h.cmd for h in load(tmp_path).hooks] == ["mvn verify"]
    assert "unknown edit command" in capsys.readouterr().err


def test_release_version_of_accepts_both_tag_forms() -> None:
    assert commands.release_version_of("fraud-juggler-1.5.0-rc0", "fraud-juggler") == "1.5.0-rc0"
    assert commands.release_version_of("1.5.0", "fraud-juggler") == "1.5.0"
    with pytest.raises(connectors.ConnectorError):
        commands.release_version_of("banana", "fraud-juggler")
