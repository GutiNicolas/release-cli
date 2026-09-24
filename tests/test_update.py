"""`release update` with a temporary HOME, a --bare remote, a fake uv, and a shim for the reinstalled `release`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from release_cli import commands, connectors, update
from release_cli.cli import main

REPO = Path(__file__).resolve().parents[1]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _pyproject(version: str) -> str:
    return f'[project]\nname = "release-cli"\nversion = "{version}"\n'


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    remote = tmp_path / "release-cli.git"
    upstream = tmp_path / "upstream"
    clone = tmp_path / "clone"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", str(upstream))
    (upstream / "pyproject.toml").write_text(_pyproject("0.1.0"), encoding="utf-8")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "0.1.0")
    git(upstream, "remote", "add", "origin", str(remote))
    git(upstream, "push", "-u", "origin", "main")
    git(tmp_path, "clone", str(remote), str(clone))
    (upstream / "pyproject.toml").write_text(_pyproject("0.2.0"), encoding="utf-8")
    git(upstream, "commit", "-am", "0.2.0")
    git(upstream, "push")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_log = tmp_path / "uv.calls"
    (bin_dir / "uv").write_text(
        f"#!{sys.executable}\nimport json, sys\nopen({str(uv_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n", encoding="utf-8"
    )
    (bin_dir / "release").write_text(
        f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(REPO)!r})\nfrom release_cli.cli import main\nmain()\n", encoding="utf-8"
    )
    for script in bin_dir.iterdir():
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return {"remote": remote, "clone": clone, "uv_log": uv_log, "elsewhere": elsewhere, "tmp": tmp_path}


def uv_calls(env: dict[str, Path]) -> list[list[str]]:
    path = env["uv_log"]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def test_update_from_another_cwd_pulls_and_reinstalls(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    update.register_source(env["clone"])
    main(["update", "--defaults"])
    clone = env["clone"]
    assert git(clone, "rev-parse", "HEAD") == git(env["remote"], "rev-parse", "main")
    assert uv_calls(env) == [["tool", "install", "--force", str(clone)]]
    out = capsys.readouterr().out
    assert "-> 0.2.0" in out and "connectors were not updated" in out
    assert Path.cwd() == env["elsewhere"]


def test_connectors_toml_and_project_release_stay_byte_for_byte(env: dict[str, Path]) -> None:
    update.register_source(env["clone"])
    connectors_toml = connectors.global_path()
    connectors_toml.parent.mkdir(parents=True, exist_ok=True)
    connectors_toml.write_text(
        '# hand-written\norder = ["platform-build"]\n\n[connectors.platform-build]\nsha = "abc"\nenabled = true\nbreak_on_error = true\n',
        encoding="utf-8",
    )
    project = env["tmp"] / "project" / ".release"
    project.parent.mkdir()
    project.write_text('tool = "maven"  # mine\nartifact = "x"\nversion_file = "pom.xml"\nhooks = []\n', encoding="utf-8")
    before = (connectors_toml.read_bytes(), project.read_bytes())
    main(["update", "--defaults"])
    assert (connectors_toml.read_bytes(), project.read_bytes()) == before


def test_new_key_is_asked_and_enter_saves_default(env: dict[str, Path]) -> None:
    prompts: list[str] = []
    asked = update.ensure_config(defaults=False, ask=lambda p: prompts.append(p) or "", log=lambda _m: None)
    assert asked == ["cursor-review.notify"]
    assert prompts == ["cursor-review: desktop notification when a post-deploy review finishes? (y/n) [n]: "]
    data = connectors.load_release_toml()
    assert data["cursor-review"]["notify"] is False and data["config_version"] == update.config_version()
    assert update.ensure_config(defaults=False, ask=lambda _p: pytest.fail("asked again")) == []


def test_new_key_with_defaults_through_update(env: dict[str, Path]) -> None:
    update.register_source(env["clone"])
    main(["update", "--defaults"])
    data = connectors.load_release_toml()
    assert data["cursor-review"] == {"notify": False}
    assert data["config_version"] == update.config_version()


def test_key_already_set_is_neither_asked_nor_changed(env: dict[str, Path]) -> None:
    connectors.save_release_toml({"cursor-review": {"notify": True}})
    assert update.ensure_config(defaults=False, ask=lambda _p: pytest.fail("asked")) == []
    data = connectors.load_release_toml()
    assert data["cursor-review"]["notify"] is True and data["config_version"] == update.config_version()


def test_unknown_hand_written_keys_stay(env: dict[str, Path]) -> None:
    path = connectors.release_toml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('mine = "keep"\n\n[cursor-review]\nextra = 3\n\n[other]\nx = true\n', encoding="utf-8")
    update.ensure_config(defaults=True)
    data = connectors.load_release_toml()
    assert data["mine"] == "keep" and data["other"] == {"x": True}
    assert data["cursor-review"] == {"extra": 3, "notify": False}


def test_changed_meaning_is_asked_showing_current_value(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    key = update.Key("cursor-review", "notify", False, "Notify?", since=1, changed=2, change_note="now also on Linux")
    monkeypatch.setattr(update, "REGISTRY", (key,))
    connectors.save_release_toml({"config_version": 1, "cursor-review": {"notify": True}})
    logs: list[str] = []
    prompts: list[str] = []
    asked = update.ensure_config(defaults=False, ask=lambda p: prompts.append(p) or "", log=logs.append)
    assert asked == ["cursor-review.notify"]
    assert "current value: True" in logs[0] and prompts == ["Notify? (y/n) [y]: "]
    assert connectors.load_release_toml() == {"config_version": 2, "cursor-review": {"notify": True}}
    assert update.ensure_config(defaults=False, ask=lambda _p: pytest.fail("asked twice")) == []


def test_dirty_clone_no_pull_no_reinstall(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    update.register_source(env["clone"])
    head = git(env["clone"], "rev-parse", "HEAD")
    (env["clone"] / "wip.txt").write_text("local change\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["update", "--defaults"])
    assert exc.value.code != 0
    assert git(env["clone"], "rev-parse", "HEAD") == head
    assert uv_calls(env) == []
    assert "local changes" in capsys.readouterr().err
    assert not connectors.release_toml_path().exists()


def test_no_source_prints_install_url_and_writes_no_config(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["update"])
    assert exc.value.code != 0
    assert update.INSTALL_URL in capsys.readouterr().err
    assert not connectors.config_dir().exists()
    assert uv_calls(env) == []


def test_uv_receipt_is_the_fallback_and_source_toml_is_written(env: dict[str, Path]) -> None:
    receipt = update.uv_tool_dir() / "release-cli" / "uv-receipt.toml"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(f'[tool]\nrequirements = [{{ name = "release-cli", directory = "{env["clone"]}" }}]\n', encoding="utf-8")
    main(["update", "--defaults"])
    assert uv_calls(env) == [["tool", "install", "--force", str(env["clone"])]]
    assert f'path = "{env["clone"].resolve()}"' in update.source_toml_path().read_text(encoding="utf-8")


def test_after_install_asks_cursor_review_only_once(env: dict[str, Path]) -> None:
    replies = iter(["y"])
    commands.after_install(env["clone"], defaults=False, ask=lambda _p: next(replies))
    assert connectors.load_release_toml()["cursor-review"]["notify"] is True
    assert "cursor-review" in connectors.load_global()["connectors"]
    source = update.source_toml_path().read_text(encoding="utf-8")
    assert 'branch = "main"' in source and str(env["remote"]) in source
    commands.after_install(env["clone"], defaults=False, ask=lambda _p: pytest.fail("asked again"))
