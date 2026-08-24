from __future__ import annotations

from pathlib import Path

from release_cli.config import parse
from release_cli.init import AutoPrompter, Prompter, initialize


class ScriptedPrompter(Prompter):
    def __init__(self, confirms: list[bool], answers: list[str] | None = None, choices: list[str] | None = None):
        self.confirms = list(confirms)
        self.answers = list(answers or [])
        self.choices = list(choices or [])

    def confirm(self, question: str, default: bool) -> bool:
        del question, default
        return self.confirms.pop(0)

    def ask(self, question: str, default: str = "") -> str:
        del question
        if self.answers:
            return self.answers.pop(0)
        return default

    def choose(self, question: str, options: list[str]) -> str:
        del question
        if self.choices:
            return self.choices.pop(0)
        return options[0]


def test_init_writes_release_and_gitignore(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        """<project>
  <artifactId>demo</artifactId>
  <version>1.0.0-SNAPSHOT</version>
</project>
""",
        encoding="utf-8",
    )
    cfg, dirtied = initialize(tmp_path, prompter=AutoPrompter(), log=lambda *_: None)
    assert cfg.tool == "maven"
    assert cfg.artifact == "demo"
    assert cfg.hooks == ()
    assert (tmp_path / ".release").is_file()
    assert ".release" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert dirtied is True
    assert not (tmp_path / "release.toml").exists()


def test_init_team_file_and_hooks(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        """<project>
  <artifactId>demo</artifactId>
  <version>1.0.0-SNAPSHOT</version>
</project>
""",
        encoding="utf-8",
    )
    prompter = ScriptedPrompter(
        confirms=[True, True, True, False, True, True],
        answers=["demo", "mvn test", "before", "mvn deploy", "after"],
    )
    cfg, dirtied = initialize(tmp_path, prompter=prompter, log=lambda *_: None)
    assert [h.cmd for h in cfg.hooks] == ["mvn test", "mvn deploy"]
    assert [h.when for h in cfg.hooks] == ["before", "after"]
    assert (tmp_path / "release.toml").is_file()
    assert dirtied is True
    loaded = parse((tmp_path / ".release").read_text(encoding="utf-8"))
    assert loaded.hooks[1].cmd == "mvn deploy"


def test_init_appends_gitignore(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        """<project>
  <artifactId>demo</artifactId>
  <version>1.0.0-SNAPSHOT</version>
</project>
""",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("target/\n", encoding="utf-8")
    initialize(tmp_path, prompter=AutoPrompter(), log=lambda *_: None)
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "target/" in text
    assert ".release" in text.splitlines()
