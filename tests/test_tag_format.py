"""`tag_format`: which tags a release creates, and that every command finds them again. Real git, bare remote."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from release_cli import commands, connectors
from release_cli.cli import main
from release_cli.config import ConfigError, dumps, load, parse
from tests.conftest import write_config
from tests.test_connectors_contract import install_fakes, run_requests
from tests.test_tag_boundary import git, local_tags, remote_tags, repo  # noqa: F401  (repo is a fixture)


def use_format(work: Path, formats: str) -> None:
    write_config(work)
    cfg = (work / ".release").read_text(encoding="utf-8")
    (work / ".release").write_text(f"tag_format = {formats}\n{cfg}", encoding="utf-8")


def test_v_tags_flow_through_release_connectors_history_notes_and_undo(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    use_format(repo, '["v{version}"]')
    install_fakes(("platform-build", "echo"))

    main(["-rc", "--defaults"])
    assert remote_tags(repo) == {"v1.5.0-rc0"} and local_tags(repo) == {"v1.5.0-rc0"}
    request = run_requests(repo.parent)[-1]
    assert (request["tag"], request["release_version"], request["tags"]) == ("v1.5.0-rc0", "1.5.0-rc0", ["v1.5.0-rc0"])
    assert connectors.load_state("example-app", "v1.5.0-rc0") is not None  # runs are saved under the tag

    (repo / "feature.txt").write_text("x\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Add the feature")
    main(["-rc", "--defaults", "--skip-connectors"])
    assert remote_tags(repo) == {"v1.5.0-rc0", "v1.5.0-rc1"}

    capsys.readouterr()
    commands.notes(repo, "v1.5.0-rc1")
    out = capsys.readouterr().out
    assert "Changes since v1.5.0-rc0" in out and "Add the feature" in out
    commands.notes(repo, None)
    assert "Changes since v1.5.0-rc0" in capsys.readouterr().out  # latest tag is found by its v form

    main(["history", "--json", "--limit", "5"])
    listed = json.loads(capsys.readouterr().out)["releases"]
    assert [r["version"] for r in listed] == ["v1.5.0-rc1", "v1.5.0-rc0"]
    assert listed[1]["connectors"]["platform-build"]["status"] == "ok"

    before = len(run_requests(repo.parent))
    main(["connectors", "run", "v1.5.0-rc0", "--defaults"])
    assert len(run_requests(repo.parent)) == before + 1
    with pytest.raises(SystemExit):
        main(["connectors", "run", "1.5.0-rc0", "--defaults"])  # no template without the v: not a tag here
    assert len(run_requests(repo.parent)) == before + 1

    commands.undo(repo, "v1.5.0-rc1", force=True)
    assert remote_tags(repo) == {"v1.5.0-rc0"}
    main(["-rc", "--defaults", "--skip-connectors"])
    assert remote_tags(repo) == {"v1.5.0-rc0", "v1.5.0-rc2"}  # the version commits stay: the next candidate takes the next number


def test_final_and_hotfix_on_v_tags(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    use_format(repo, '["v{version}"]')
    main(["-rc", "--defaults", "--skip-connectors"])
    main(["-fv", "--defaults", "--skip-connectors"])
    assert remote_tags(repo) == {"v1.5.0-rc0", "v1.5.0"}
    final_commit = git(repo, "rev-parse", "v1.5.0^{commit}")

    main(["hotfix", "v1.5.0"])
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "hotfix/1.5.1"
    assert git(repo, "rev-parse", "HEAD") == final_commit
    main(["-rc", "--defaults", "--skip-connectors"])
    main(["-fv", "--defaults", "--skip-connectors"])
    assert {"v1.5.1-rc0", "v1.5.1"} <= remote_tags(repo)

    git(repo, "switch", "main")
    git(repo, "branch", "-D", "hotfix/1.5.1")
    with pytest.raises(SystemExit):
        main(["hotfix", "v1.5.0"])
    assert "1.5.1 already has tags (v1.5.1, v1.5.1-rc0); patch from the latest one instead: release hotfix v1.5.1" in capsys.readouterr().err


def test_two_templates_create_both_and_either_form_names_the_release(repo: Path) -> None:  # noqa: F811
    use_format(repo, '["v{version}", "{artifact}-{version}"]')
    main(["-rc", "--defaults", "--skip-connectors"])
    assert remote_tags(repo) == {"v1.5.0-rc0", "example-app-1.5.0-rc0"}

    commands.undo(repo, "example-app-1.5.0-rc0", force=True)
    assert remote_tags(repo) == set() and local_tags(repo) == set()


TOOL = 'tool = "maven"\nartifact = "example-app"\nversion_file = "pom.xml"\n'


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("[]", "non-empty list"),
        ('"v{version}"', "non-empty list"),
        ('["v"]', r"\{version\} exactly once"),
        ('["{version}-{version}"]', r"\{version\} exactly once"),
        ('["v{version}", "v{version}"]', "twice"),
        ('["{version}{tag}"]', "unknown placeholder"),
        ('["{artifact}-{artifact}-{version}"]', "more than once"),
        ('["v {version}"]', "not a valid tag name"),
        ("[1]", "non-empty strings"),
        ('["{version}.lock"]', "git cannot tag"),
        ('["/{version}"]', "git cannot tag"),
        ('["{version}/"]', "git cannot tag"),
        ('["-{version}"]', "git cannot tag"),
    ],
)
def test_invalid_templates_are_refused_at_load_with_the_reason(value: str, reason: str) -> None:
    with pytest.raises(ConfigError, match=reason):
        parse(f"{TOOL}tag_format = {value}\n")


def test_tag_format_survives_a_write_and_the_team_file_is_not_clobbered_by_the_local_one(tmp_path: Path) -> None:
    cfg = parse(f'{TOOL}tag_format = ["v{{version}}"]\n')
    assert parse(dumps(cfg)).tag_format == ("v{version}",)
    (tmp_path / "release.toml").write_text(dumps(cfg), encoding="utf-8")
    (tmp_path / ".release").write_text(f"{TOOL}hooks = []\n", encoding="utf-8")  # a local file that says nothing about tags
    merged = load(tmp_path)
    assert merged is not None and merged.tags.render("1.0.0") == ["v1.0.0"]
    assert parse(TOOL).tags.render("1.0.0") == ["1.0.0", "example-app-1.0.0"]  # unset keeps today's two tags
