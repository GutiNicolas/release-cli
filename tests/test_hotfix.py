"""`release hotfix`: real git with a bare remote. A patch on a released final ships as X.Y.(Z+1)-rcN, then X.Y.(Z+1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from release_cli.cli import main
from release_cli.versioning import ProjectVersion, plan
from tests.conftest import write_config
from tests.test_maven_modules import pom
from tests.test_tag_boundary import git, local_tags, remote_tags
from tests.test_tag_boundary import repo as repo  # noqa: F401  (pytest fixture)


def released_1_5_0(repo: Path) -> None:
    main(["-rc", "--defaults"])
    main(["-fv", "--defaults"])


def branches(work: Path) -> set[str]:
    return set(git(work, "branch", "--format=%(refname:short)").split())


def test_plan_default_bump_only_applies_to_a_new_series() -> None:
    final = ProjectVersion(2, 74, 0)
    assert plan(final, final=False).release.format() == "2.75.0-rc0"
    assert plan(final, final=False, default_bump="patch").release.format() == "2.74.1-rc0"
    assert plan(final, final=False, bump="minor", default_bump="patch").release.format() == "2.75.0-rc0"
    open_rc = ProjectVersion(2, 74, 1, rc=0, snapshot=True)
    assert plan(open_rc, final=False, default_bump="patch").release.format() == "2.74.1-rc1"


def test_hotfix_ships_patch_rcs_then_final_and_main_is_untouched(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    released_1_5_0(repo)
    main_head = git(repo, "rev-parse", "origin/main")
    final_commit = git(repo, "rev-parse", "1.5.0^{commit}")

    main(["hotfix", "1.5.0"])
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "hotfix/1.5.1"
    assert git(repo, "rev-parse", "HEAD") == final_commit
    assert "<version>1.5.0</version>" in (repo / "pom.xml").read_text(encoding="utf-8")

    capsys.readouterr()
    main(["-rc", "--defaults"])
    assert "HOTFIX BRANCH hotfix/1.5.1" in capsys.readouterr().out
    assert "<version>1.5.1-rc0-SNAPSHOT</version>" in (repo / "pom.xml").read_text(encoding="utf-8")
    main(["-rc", "--defaults"])
    main(["-fv", "--defaults"])
    out = capsys.readouterr().out

    assert {"1.5.1-rc0", "1.5.1-rc1", "1.5.1", "example-app-1.5.1"} <= remote_tags(repo)
    assert "next `release -rc` starts 1.5.2-rc0" in out
    assert "merge hotfix/1.5.1 back into your main branch" in out and "keep the main branch's version" in out
    assert git(repo, "rev-parse", "origin/main") == main_head  # nothing merged for the user
    assert git(repo, "rev-parse", "origin/hotfix/1.5.1") == git(repo, "rev-parse", "HEAD")

    git(repo, "switch", "main")
    capsys.readouterr()
    main(["-rc", "--dry-run", "--defaults"])
    assert "RELEASE VERSION: 1.6.0-rc0" in capsys.readouterr().out  # the patch default is scoped to hotfix/*


def test_hotfix_fetches_a_tag_that_only_origin_has(repo: Path) -> None:
    released_1_5_0(repo)
    git(repo, "tag", "-d", "1.5.0", "example-app-1.5.0")
    assert "1.5.0" not in local_tags(repo)

    main(["hotfix", "example-app-1.5.0"])  # also accepts the artifact-prefixed tag

    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "hotfix/1.5.1"
    assert "1.5.0" in local_tags(repo)


def _dirty(repo: Path) -> str:
    (repo / "pom.xml").write_text((repo / "pom.xml").read_text(encoding="utf-8") + "<!-- wip -->\n", encoding="utf-8")
    return "1.5.0"


def _existing_local_branch(repo: Path) -> str:
    git(repo, "branch", "hotfix/1.5.1")
    return "1.5.0"


def _existing_remote_branch(repo: Path) -> str:
    git(repo, "push", "origin", "main:refs/heads/hotfix/1.5.1")
    return "1.5.0"


def _already_patched(repo: Path) -> str:
    git(repo, "tag", "-a", "1.5.1-rc0", "-m", "x")
    return "1.5.0"


def _rc_tag(repo: Path) -> str:
    git(repo, "tag", "-a", "1.4.9-rc3", "-m", "x")
    return "1.4.9-rc3"


def _not_a_release_commit(repo: Path) -> str:
    first = git(repo, "rev-list", "--max-parents=0", "HEAD")
    git(repo, "tag", "-a", "1.4.0", first, "-m", "x")  # a tag whose pom reads 1.4.0-SNAPSHOT
    return "1.4.0"


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        (_dirty, "working tree is not clean"),
        (_existing_local_branch, "branch hotfix/1.5.1 already exists"),
        (_existing_remote_branch, "branch hotfix/1.5.1 already exists"),
        (_already_patched, "1.5.1 already has tags"),
        (_rc_tag, "not a final release"),
        (lambda repo: "9.9.9", "not found locally or on origin"),
        (_not_a_release_commit, "version file reads 1.4.0-SNAPSHOT"),
    ],
    ids=["dirty", "local-branch", "remote-branch", "already-patched", "rc-tag", "missing-tag", "not-a-release-commit"],
)
def test_hotfix_refuses_and_leaves_the_repo_as_it_was(
    repo: Path, prepare, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    released_1_5_0(repo)
    tag = prepare(repo)
    before = (git(repo, "rev-parse", "--abbrev-ref", "HEAD"), git(repo, "rev-parse", "HEAD"), branches(repo))

    with pytest.raises(SystemExit):
        main(["hotfix", tag])

    assert message in capsys.readouterr().err
    assert (git(repo, "rev-parse", "--abbrev-ref", "HEAD"), git(repo, "rev-parse", "HEAD"), branches(repo)) == before


GRADLE = {
    "gradle.properties": "version=1.4.0-SNAPSHOT\n",
    "settings.gradle": 'rootProject.name = "example-app"\n',
}
SBT = {"build.sbt": 'name := "example-app"\nversion := "1.4.0-SNAPSHOT"\n'}
MODULES = {
    "pom.xml": pom("example-app", version="1.4.0-SNAPSHOT", group="com.acme", modules=("api",)),
    "api/pom.xml": pom("api", parent=("com.acme", "example-app", "1.4.0-SNAPSHOT")),
}


@pytest.mark.parametrize(
    ("tool", "version_file", "files", "expect"),
    [
        ("gradle", "gradle.properties", GRADLE, "version=1.5.1-rc0-SNAPSHOT"),
        ("sbt", "build.sbt", SBT, 'version := "1.5.1-rc0-SNAPSHOT"'),
        ("maven", "pom.xml", MODULES, "<version>1.5.1-rc0-SNAPSHOT</version>"),
    ],
    ids=["gradle", "sbt", "maven-multi-module"],
)
def test_hotfix_on_every_build_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, version_file: str, files: dict[str, str], expect: str
) -> None:
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote, work = tmp_path / "example-app.git", tmp_path / "work"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", str(work))
    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text, encoding="utf-8")
    (work / ".gitignore").write_text(".release\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    write_config(work, tool=tool, version_file=version_file)
    monkeypatch.chdir(work)

    main(["-rc", "--defaults"])
    main(["-fv", "--defaults"])
    main(["hotfix", "1.5.0"])
    main(["-rc", "--defaults"])

    assert {"1.5.0", "1.5.1-rc0"} <= remote_tags(work)
    assert expect in (work / version_file).read_text(encoding="utf-8")
    if tool == "maven":  # the module follows its parent
        assert "<version>1.5.1-rc0-SNAPSHOT</version>" in (work / "api/pom.xml").read_text(encoding="utf-8")
