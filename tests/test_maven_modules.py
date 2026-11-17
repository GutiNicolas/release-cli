from __future__ import annotations

from pathlib import Path

from release_cli.adapters.maven import MavenAdapter
from release_cli.config import Config

CFG = Config(tool="maven", artifact="root", version_file="pom.xml")


def pom(
    artifact: str,
    *,
    parent: tuple[str, str, str] | None = None,
    version: str | None = None,
    modules: tuple[str, ...] = (),
    group: str | None = None,
) -> str:
    out = ['<project xmlns="http://maven.apache.org/POM/4.0.0">', "  <modelVersion>4.0.0</modelVersion>"]
    if parent:
        g, a, v = parent
        out += [
            "  <parent>",
            f"    <groupId>{g}</groupId>",
            f"    <artifactId>{a}</artifactId>",
            f"    <version>{v}</version>",
            "  </parent>",
        ]
    if group:
        out.append(f"  <groupId>{group}</groupId>")
    out.append(f"  <artifactId>{artifact}</artifactId>")
    if version:
        out.append(f"  <version>{version}</version>")
    if modules:
        out += ["  <modules>", *(f"    <module>{m}</module>" for m in modules), "  </modules>"]
    out += [
        "  <dependencies>",
        "    <dependency><artifactId>x</artifactId><version>1.0.0</version></dependency>",
        "  </dependencies>",
        "</project>",
        "",
    ]
    return "\n".join(out)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_nested_aggregator_bumps_every_parent_version_and_nothing_else(tmp_path: Path) -> None:
    root_parent = ("com.acme", "root", "1.0.0")
    # libs has no own <version>: it inherits the root's, so core (child of libs) must follow too.
    write(tmp_path / "pom.xml", pom("root", version="1.0.0", group="com.acme", modules=("api", "libs")))
    write(tmp_path / "api/pom.xml", pom("api", parent=root_parent))
    write(tmp_path / "libs/pom.xml", pom("libs", parent=root_parent, modules=("core",)))
    write(tmp_path / "libs/core/pom.xml", pom("core", parent=("com.acme", "libs", "1.0.0")))
    before = {p: p.read_text() for p in tmp_path.rglob("pom.xml")}

    changed = MavenAdapter().write(tmp_path, CFG, "1.1.0")

    assert changed[0] == tmp_path / "pom.xml"
    assert set(changed) == set(before)
    assert set(MavenAdapter().paths(tmp_path, CFG)) == set(before)
    for p, old in before.items():
        # exactly the one version line changed (dependency 1.0.0 stays, even in the root)
        expected = old.replace("<version>1.0.0</version>\n", "<version>1.1.0</version>\n", 1)
        assert p.read_text() == expected
        assert "<artifactId>x</artifactId><version>1.0.0</version>" in p.read_text()


def test_foreign_parent_or_other_version_is_untouched(tmp_path: Path) -> None:
    write(tmp_path / "pom.xml", pom("root", version="1.0.0", group="com.acme", modules=("boot", "stale")))
    boot = pom("boot", parent=("org.springframework.boot", "spring-boot-starter-parent", "1.0.0"))
    stale = pom("stale", parent=("com.acme", "root", "0.9.0"))
    write(tmp_path / "boot/pom.xml", boot)
    write(tmp_path / "stale/pom.xml", stale)

    changed = MavenAdapter().write(tmp_path, CFG, "2.0.0")

    assert changed == [tmp_path / "pom.xml"]
    assert (tmp_path / "boot/pom.xml").read_text() == boot
    assert (tmp_path / "stale/pom.xml").read_text() == stale


def test_child_with_explicit_equal_version_is_bumped(tmp_path: Path) -> None:
    write(tmp_path / "pom.xml", pom("root", version="1.0.0", group="com.acme", modules=("m",)))
    write(tmp_path / "m/pom.xml", pom("m", parent=("com.acme", "root", "1.0.0"), version="1.0.0"))

    MavenAdapter().write(tmp_path, CFG, "1.0.1")

    text = (tmp_path / "m/pom.xml").read_text()
    assert text.count("<version>1.0.1</version>") == 2
    assert "<version>1.0.0</version></dependency>" in text


def test_root_inheriting_group_from_its_parent_and_crlf_preserved(tmp_path: Path) -> None:
    root = pom("root", parent=("com.acme", "bom", "5"), version="1.0.0", modules=("m",))
    child = pom("m", parent=("com.acme", "root", "1.0.0")).replace("\n", "\r\n")
    write(tmp_path / "pom.xml", root)
    (tmp_path / "m").mkdir()
    (tmp_path / "m/pom.xml").write_bytes(child.encode())

    MavenAdapter().write(tmp_path, CFG, "1.0.1")

    out = (tmp_path / "m/pom.xml").read_bytes()
    assert out == child.replace("<version>1.0.0</version>", "<version>1.0.1</version>", 1).encode()
    assert b"\r\n" in out


def test_missing_module_dir_is_skipped(tmp_path: Path) -> None:
    write(tmp_path / "pom.xml", pom("root", version="1.0.0", group="g", modules=("gone", "here")))
    write(tmp_path / "here/pom.xml", pom("here", parent=("g", "root", "1.0.0")))

    changed = MavenAdapter().write(tmp_path, CFG, "1.0.1")

    assert changed == [tmp_path / "pom.xml", tmp_path / "here/pom.xml"]
    assert MavenAdapter().paths(tmp_path, CFG) == changed
