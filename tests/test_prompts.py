from __future__ import annotations

from pathlib import Path

import pytest

from release_cli import prompts
from release_cli.config import dump_toml, load, loads_toml, parse, set_connector_value
from tests.conftest import write_config


def _answers(*values: str):
    it = iter(values)
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return next(it)

    return ask, asked


def test_bool_reasks_garbage_and_enter_is_default() -> None:
    ask, asked = _answers("maybe", "")
    assert prompts.ask_bool("Go?", False, ask=ask) is False
    assert len(asked) == 2
    assert asked[0] == "Go? (y/n) [n]: "


def test_choice_reasks_until_option() -> None:
    ask, asked = _answers("prod", "dev")
    assert prompts.ask_choice("Env", ["qa", "dev"], "qa", ask=ask) == "dev"
    assert len(asked) == 2


def test_text_required_reasks_empty() -> None:
    ask, asked = _answers("", "  DEMO ")
    assert prompts.ask_text("Jira key", ask=ask) == "DEMO"
    assert len(asked) == 2


def test_defaults_never_invents_text() -> None:
    assert prompts.ask_bool("Go?", False, defaults=True) is False
    assert prompts.ask_choice("Env", ["qa", "dev"], "dev", defaults=True) == "dev"
    assert prompts.ask_text("Extra", required=False, defaults=True) == ""
    with pytest.raises(prompts.PromptError):
        prompts.ask_text("Jira key", defaults=True)


def test_toml_writer_round_trips() -> None:
    data = {
        "tool": "maven",
        "hooks": [{"when": "before", "cmd": 'say "hi"', "default": False}],
        "connectors": {"order": ["b", "a"], "platform-deploy": {"jira_project_key": "DEMO", "n": 3}},
        "empty": [],
    }
    assert loads_toml(dump_toml(data)) == data


def test_set_connector_value_keeps_hooks_and_team_layer(tmp_path: Path) -> None:
    write_config(tmp_path, hooks='[[hooks]]\nwhen = "before"\ncmd = "mvn test"\n')
    (tmp_path / "release.toml").write_text(
        'tool = "maven"\nartifact = "x"\nversion_file = "pom.xml"\n[connectors.platform-build]\nversion_tag = "plain"\n',
        encoding="utf-8",
    )
    set_connector_value(tmp_path, "platform-deploy", "jira_project_key", "DEMO")
    cfg = load(tmp_path)
    assert cfg is not None
    assert cfg.hooks[0].cmd == "mvn test"
    assert cfg.connectors["platform-deploy"] == {"jira_project_key": "DEMO"}
    assert cfg.connectors["platform-build"] == {"version_tag": "plain"}
    assert parse((tmp_path / ".release").read_text(encoding="utf-8")).artifact == "example-app"
