"""`release history` on real git: version tags newest first, joined with the saved connector runs."""

from __future__ import annotations

import json

import pytest

from release_cli import connectors
from release_cli.cli import main
from tests.test_connectors_contract import install_fakes
from tests.test_tag_boundary import _swap, git, repo  # noqa: F401  (repo is a fixture)


@pytest.fixture()
def released(repo) -> None:  # noqa: F811
    """1.5.0-rc0 (build failed), 1.5.0-rc1 and 1.5.0 (all ok); rc1's saved runs are then deleted."""
    install_fakes(("platform-build", "fail"))
    with pytest.raises(SystemExit):
        main(["-rc", "--defaults"])
    _swap("platform-build", "echo")
    main(["-rc", "--defaults"])
    main(["-fv", "--defaults"])
    connectors.state_path("example-app", "1.5.0-rc1").unlink()


def test_lists_newest_first_with_connector_runs_and_resume_hint(released: None, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    main(["history"])
    out = capsys.readouterr().out
    assert out.index("1.5.0  ") < out.index("1.5.0-rc1") < out.index("1.5.0-rc0")
    assert "platform-build: ok" in out
    assert "no connector runs on this machine" in out  # rc1: state deleted
    failed = out[out.index("1.5.0-rc0") :]
    assert "platform-build: failed" in failed
    assert "release connectors run 1.5.0-rc0 --from platform-build" in failed
    assert "resume" not in out[: out.index("1.5.0-rc0")]


def test_finals_and_limit(released: None, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    main(["history", "--finals", "--json"])
    assert [r["version"] for r in json.loads(capsys.readouterr().out)["releases"]] == ["1.5.0"]
    main(["history", "--limit", "2", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert [r["version"] for r in data["releases"]] == ["1.5.0", "1.5.0-rc1"]


def test_json_carries_commit_and_connector_detail(released: None, repo, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    capsys.readouterr()
    main(["history", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["repo"] == "example-app"
    rc1, rc0 = data["releases"][1], data["releases"][2]
    assert rc1["connectors"] is None
    assert rc0["commit"] == git(repo, "rev-parse", "1.5.0-rc0^{commit}")
    build = rc0["connectors"]["platform-build"]
    assert build["status"] == "failed" and build["updated_at"] and build["resume"].endswith("--from platform-build")
    assert "resume" not in data["releases"][0]["connectors"]["platform-build"]
