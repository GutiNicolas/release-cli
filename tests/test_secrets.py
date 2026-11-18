"""Secrets: lookup order, `release secret set|ls|rm`, and that a value never reaches output or a command line.

`security` and `secret-tool` are faked by scripts on PATH backed by a file in tmp_path; the real keychain is never touched.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from release_cli import secrets
from release_cli.cli import main

VALUE = "s3cr3t-VALUE-xyz"

SECURITY = f"""#!{sys.executable}
import os, sys
store, log = os.environ["FAKE_STORE"] + ".security", os.environ["FAKE_LOG"]
open(log, "a").write(" ".join(sys.argv[1:]) + "\\n")
def read():
    try:
        return dict(l.rstrip("\\n").split("=", 1) for l in open(store))
    except OSError:
        return {{}}
def write(d):
    open(store, "w").write("".join(f"{{k}}={{v}}\\n" for k, v in d.items()))
def run(args, data):
    cmd, d = args[0], read()
    account = args[args.index("-a") + 1]
    if cmd == "add-generic-password":
        d[account] = bytes.fromhex(args[args.index("-X") + 1]).decode()
        write(d)
    elif cmd == "find-generic-password":
        if account not in d:
            sys.exit(44)
        print(d[account])
    elif cmd == "delete-generic-password":
        if account not in d:
            sys.exit(44)
        del d[account]
        write(d)
if sys.argv[1] == "-i":
    for line in sys.stdin.read().splitlines():
        run(line.split(), None)
else:
    run(sys.argv[1:], None)
"""

SECRET_TOOL = f"""#!{sys.executable}
import os, sys
store, log = os.environ["FAKE_STORE"] + ".secret-tool", os.environ["FAKE_LOG"]
open(log, "a").write(" ".join(sys.argv[1:]) + "\\n")
cmd, account = sys.argv[1], sys.argv[-1]
try:
    d = dict(l.rstrip("\\n").split("=", 1) for l in open(store))
except OSError:
    d = {{}}
if cmd == "store":
    d[account] = sys.stdin.read()
elif cmd == "lookup":
    if account not in d:
        sys.exit(1)
    print(d[account])
elif cmd == "clear":
    d.pop(account, None)
open(store, "w").write("".join(f"{{k}}={{v}}\\n" for k, v in d.items()))
"""


def fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *tools: str) -> Path:
    """Put the named fakes alone on PATH; returns the file recording every argv they were given."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in tools:
        script = bin_dir / tool
        script.write_text(SECURITY if tool == "security" else SECRET_TOOL, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("FAKE_LOG", str(log))
    return log


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("RELEASE_SECRET_JIRA_TOKEN", raising=False)


def release(*argv: str) -> int:
    try:
        main(list(argv))
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_get_order_env_then_keychain_then_secret_tool_then_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tools(tmp_path, monkeypatch, "security", "secret-tool")
    assert secrets.get("jira-token") is None
    (tmp_path / "store.secret-tool").write_text("jira-token=from-secret-tool\n", encoding="utf-8")
    assert secrets.get("jira-token") == "from-secret-tool"
    (tmp_path / "store.security").write_text("jira-token=from-keychain\n", encoding="utf-8")
    assert secrets.get("jira-token") == "from-keychain"
    monkeypatch.setenv("RELEASE_SECRET_JIRA_TOKEN", "from-env")
    assert secrets.get("jira-token") == "from-env"


def test_get_without_any_keychain_tool_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert secrets.get("jira-token") is None


def test_set_ls_rm_roundtrip_never_shows_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = fake_tools(tmp_path, monkeypatch, "security")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": VALUE)

    assert release("secret", "set", "jira-token") == 0
    assert secrets.get("jira-token") == VALUE
    assert VALUE not in log.read_text(encoding="utf-8")  # not on any command line

    assert release("secret", "ls") == 0
    assert capsys.readouterr().out.split()[-1] == "jira-token"  # last line of the set + ls output

    assert release("secret", "rm", "jira-token") == 0
    assert secrets.get("jira-token") is None
    assert release("secret", "ls") == 0
    assert "jira-token" not in capsys.readouterr().out.replace("removed jira-token", "")
    assert release("secret", "rm", "jira-token") == 1  # already gone

    out = capsys.readouterr()
    assert VALUE not in out.out + out.err


def test_set_on_linux_uses_secret_tool_with_value_on_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = fake_tools(tmp_path, monkeypatch, "secret-tool")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": VALUE)
    assert release("secret", "set", "slack-webhook") == 0
    assert secrets.get("slack-webhook") == VALUE
    assert VALUE not in log.read_text(encoding="utf-8")


def test_set_without_keychain_points_to_the_env_var(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": VALUE)
    assert release("secret", "set", "jira-token") == 1
    err = capsys.readouterr().err
    assert "RELEASE_SECRET_JIRA_TOKEN" in err and VALUE not in err


def test_ls_includes_names_given_by_environment(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RELEASE_SECRET_SLACK_WEBHOOK", VALUE)
    assert release("secret", "ls") == 0
    out = capsys.readouterr().out
    assert out.split() == ["slack-webhook"] and VALUE not in out


@pytest.mark.parametrize("name", ["Jira Token", "../x", "-x", ""])
def test_invalid_names_are_refused(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert release("secret", "rm", name) != 0
