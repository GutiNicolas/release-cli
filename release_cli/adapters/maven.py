"""Maven adapter: root /project/version + /project/scm/tag, and child modules' parent/own version."""

from __future__ import annotations

from pathlib import Path

from release_cli.adapters.base import AdapterError, ProjectState
from release_cli.config import Config
from release_cli.pom import (
    PomError,
    apply_child_version,
    apply_version,
    read_fields,
    read_refs,
)

POM = "pom.xml"


def _module_pom(base: Path, module: str) -> Path:
    target = base.parent / module
    return target if target.suffix == ".xml" else target / POM


def _walk_modules(pom: Path, seen: set[Path]) -> list[Path]:
    """Module poms listed (recursively) by `pom`, in document order; missing ones skipped."""
    found: list[Path] = []
    try:
        refs = read_refs(pom.read_text(encoding="utf-8"))
    except (PomError, OSError):
        return found
    for name, _line in refs.modules:
        child = _module_pom(pom, name)
        key = child.resolve()
        if key in seen or not child.is_file():
            continue
        seen.add(key)
        found.append(child)
        found.extend(_walk_modules(child, seen))
    return found


class MavenAdapter:
    name = "maven"

    def _root(self, cwd: Path, cfg: Config) -> Path:
        return cwd / (cfg.version_file or POM)

    def discover(self, cwd: Path) -> ProjectState:
        return self.read(cwd, Config(tool="maven", artifact="unused", version_file=POM))

    def read(self, cwd: Path, cfg: Config) -> ProjectState:
        path = self._root(cwd, cfg)
        if not path.is_file():
            raise AdapterError(f"{path.name} not found")
        try:
            fields = read_fields(path.read_text(encoding="utf-8"))
        except PomError as exc:
            raise AdapterError(str(exc)) from exc
        return ProjectState(
            version=fields.version,
            artifact=fields.artifact_id,
            version_file=path.name,
        )

    def paths(self, cwd: Path, cfg: Config) -> list[Path]:
        root = self._root(cwd, cfg)
        return [root, *_walk_modules(root, {root.resolve()})]

    def write(
        self,
        cwd: Path,
        cfg: Config,
        version: str,
        *,
        scm_tag: str | None = None,
    ) -> list[Path]:
        root = self._root(cwd, cfg)
        original = root.read_text(encoding="utf-8")
        try:
            old_version = read_fields(original).version
            refs = read_refs(original)
            root.write_text(apply_version(original, version, scm_tag), encoding="utf-8")
            changed = [root]
            bumped = {(refs.group_id, refs.artifact_id)}
            for child in _walk_modules(root, {root.resolve()}):
                with child.open(encoding="utf-8", newline="") as fh:
                    text = fh.read()
                updated = apply_child_version(text, bumped, old_version, version)
                if updated != text:
                    # Modules inheriting the version from this one now carry the new version.
                    child_refs = read_refs(text)
                    if child_refs.version in (None, old_version):
                        bumped.add((child_refs.group_id, child_refs.artifact_id))
                    with child.open("w", encoding="utf-8", newline="") as fh:
                        fh.write(updated)
                    changed.append(child)
        except PomError as exc:
            raise AdapterError(str(exc)) from exc
        return changed
