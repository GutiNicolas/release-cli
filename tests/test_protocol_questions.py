from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from release_cli.config import load
from release_cli.connectors import Connector, ConnectorError, MissingAnswer, ProtocolError, collect_answers, parse_questions
from tests.conftest import write_config

DEPLOY = Connector(name="platform-deploy", command=[])
PROD_QUESTIONS: list[dict[str, Any]] = [
    {"id": "environment", "type": "choice", "prompt": "Environment", "options": ["qa", "dynamic", "prd", "ops"], "default": "qa"},
    {
        "id": "deploy_type",
        "type": "choice",
        "prompt": "Deploy type",
        "options": ["SAFE_DEPLOY", "ROLLING_UPDATE", "WEIGHTED_ROLLING_UPDATE"],
        "default": "ROLLING_UPDATE",
        "default_when": [{"if": {"environment": ["prd", "ops"]}, "default": "SAFE_DEPLOY"}],
    },
    {
        "id": "skip_safe_deploy_reason",
        "type": "text",
        "persist": "none",
        "prompt": "Why not Safe Deploy?",
        "when": {"environment": ["prd", "ops"], "deploy_type": ["ROLLING_UPDATE", "WEIGHTED_ROLLING_UPDATE"]},
    },
    {"id": "pods", "type": "int", "prompt": "Pods", "min": 1, "max": 50, "default": 2, "when": {"deploy_type": "WEIGHTED_ROLLING_UPDATE"}},
    {"id": "jira_project_key", "type": "text", "prompt": "Jira key", "pattern": "[A-Z][A-Z0-9]+", "when": {"environment": ["prd", "ops"]}},
]


def _qs(items: list[dict[str, Any]]):
    return parse_questions({"protocol": 1, "questions": items}).items


def _collect(tmp_path: Path, replies: list[str], *, defaults: bool = False, items=PROD_QUESTIONS, stored: dict | None = None):
    write_config(tmp_path)
    it = iter(replies)
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return next(it)

    answers = collect_answers(DEPLOY, _qs(items), cwd=tmp_path, project_values=dict(stored or {}), defaults=defaults, ask=ask)
    return answers, asked


@pytest.mark.parametrize(
    ("environment", "expected"),
    [("prd", "SAFE_DEPLOY"), ("ops", "SAFE_DEPLOY"), ("qa", "ROLLING_UPDATE"), ("dynamic", "ROLLING_UPDATE")],
)
def test_default_when_picks_deploy_type_by_environment(tmp_path: Path, environment: str, expected: str) -> None:
    replies = [environment, ""] + (["DEMO"] if environment in ("prd", "ops") else [])
    answers, asked = _collect(tmp_path, replies)
    assert answers["deploy_type"] == expected
    assert f"[{expected}]" in asked[1]
    assert "skip_safe_deploy_reason" not in answers


def test_reason_asked_only_for_prod_without_safe_deploy(tmp_path: Path) -> None:
    answers, asked = _collect(tmp_path, ["prd", "ROLLING_UPDATE", "", "hotfix, canary not needed", "DEMO"])
    assert answers["skip_safe_deploy_reason"] == "hotfix, canary not needed"
    assert len(asked) == 5
    qa, _ = _collect(tmp_path, ["qa", "ROLLING_UPDATE"])
    assert "skip_safe_deploy_reason" not in qa and "jira_project_key" not in qa


def test_defaults_uses_default_when(tmp_path: Path) -> None:
    items = [dict(PROD_QUESTIONS[0], default="prd"), PROD_QUESTIONS[1]]
    answers, asked = _collect(tmp_path, [], defaults=True, items=items)
    assert answers == {"environment": "prd", "deploy_type": "SAFE_DEPLOY"}
    assert asked == []


def test_pattern_reasks_and_persists_valid_value(tmp_path: Path) -> None:
    answers, asked = _collect(tmp_path, ["prd", "", "demo", "DEMO"])
    assert answers["jira_project_key"] == "DEMO"
    assert len(asked) == 4
    cfg = load(tmp_path)
    assert cfg is not None and cfg.connectors["platform-deploy"]["jira_project_key"] == "DEMO"


def test_invalid_stored_value_reasked_and_fails_under_defaults(tmp_path: Path) -> None:
    items = [PROD_QUESTIONS[0], PROD_QUESTIONS[4]]
    answers, asked = _collect(tmp_path, ["prd", "FR1"], items=items, stored={"jira_project_key": "bad key"})
    assert answers["jira_project_key"] == "FR1"
    assert len(asked) == 2
    with pytest.raises(MissingAnswer, match="release edit config set platform-deploy jira_project_key"):
        _collect(tmp_path, [], defaults=True, items=[dict(items[0], default="prd"), items[1]], stored={"jira_project_key": "bad key"})


def test_int_range_reasks_and_returns_int(tmp_path: Path) -> None:
    replies = ["qa", "WEIGHTED_ROLLING_UPDATE", "zero", "0", "51", "7"]
    answers, asked = _collect(tmp_path, replies)
    assert answers["pods"] == 7
    assert len(asked) == 6


def test_int_without_default_fails_under_defaults(tmp_path: Path) -> None:
    items = [{"id": "hours", "type": "int", "prompt": "Hours", "min": 1, "max": 5}]
    with pytest.raises(MissingAnswer, match="run without --defaults"):
        _collect(tmp_path, [], defaults=True, items=items)
    optional = [dict(items[0], required=False)]
    answers, _ = _collect(tmp_path, [""], items=optional)
    assert answers == {}


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        ({"id": "n", "type": "int", "prompt": "N", "min": 1, "max": 5, "default": 9}, "default"),
        ({"id": "n", "type": "int", "prompt": "N", "min": 5, "max": 1}, "min is greater"),
        ({"id": "n", "type": "int", "prompt": "N", "default": "2"}, "default"),
        ({"id": "k", "type": "text", "prompt": "K", "pattern": "[A-Z]+", "default": "abc"}, "default"),
        ({"id": "k", "type": "text", "prompt": "K", "pattern": "("}, "invalid pattern"),
        ({"id": "c", "type": "choice", "prompt": "C", "options": ["a"], "default": "a", "default_when": [{"if": {}, "default": "z"}]}, "default_when"),
        ({"id": "c", "type": "choice", "prompt": "C", "options": ["a"], "default": "a", "default_when": [{"if": {"later": "x"}, "default": "a"}]}, "earlier"),
        ({"id": "c", "type": "choice", "prompt": "C", "options": ["a"], "default": "a", "default_when": [{"when": {}, "default": "a"}]}, "default_when"),
    ],
)
def test_invalid_question_schema_rejected(item: dict[str, Any], reason: str) -> None:
    with pytest.raises(ProtocolError, match=reason):
        _qs([item])


def test_missing_answer_is_a_connector_error() -> None:
    assert issubclass(MissingAnswer, ConnectorError)
