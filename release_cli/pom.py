"""Locate /project/version, /project/artifactId, /project/scm/tag and splice those lines only."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass


class PomError(ValueError):
    pass


@dataclass(frozen=True)
class PomFields:
    version: str
    artifact_id: str
    scm_tag: str | None
    version_line: int
    artifact_line: int
    scm_tag_line: int | None


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _direct(parent: ET.Element, name: str) -> ET.Element | None:
    for child in list(parent):
        if _local(child.tag) == name:
            return child
    return None


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    return el.text.strip()


def read_fields(xml: str) -> PomFields:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise PomError(f"invalid pom.xml: {exc}") from exc
    if _local(root.tag) != "project":
        raise PomError("pom.xml root is not <project>")

    version_el = _direct(root, "version")
    artifact_el = _direct(root, "artifactId")
    if version_el is None or _text(version_el) is None:
        raise PomError("missing /project/version")
    if artifact_el is None or _text(artifact_el) is None:
        raise PomError("missing /project/artifactId")

    scm = _direct(root, "scm")
    tag_el = _direct(scm, "tag") if scm is not None else None
    scm_tag = _text(tag_el)

    lines = _project_child_lines(xml)
    if "version" not in lines:
        raise PomError("could not locate /project/version line")
    if "artifactId" not in lines:
        raise PomError("could not locate /project/artifactId line")
    return PomFields(
        version=_text(version_el) or "",
        artifact_id=_text(artifact_el) or "",
        scm_tag=scm_tag,
        version_line=lines["version"],
        artifact_line=lines["artifactId"],
        scm_tag_line=lines.get("scm.tag"),
    )


_TAG = re.compile(r"<(/)?(?:([\w.-]+):)?([\w.-]+)([^>]*?)(/)?>")


def _project_child_lines(xml: str) -> dict[str, int]:
    """1-based original line numbers for project-level tags. Comments/PI skipped."""
    stack: list[str] = []
    hits: dict[str, int] = {}
    i = 0
    line = 1
    n = len(xml)
    while i < n:
        if xml.startswith("<!--", i):
            end = xml.find("-->", i + 4)
            if end < 0:
                break
            line += xml.count("\n", i, end + 3)
            i = end + 3
            continue
        if xml.startswith("<![CDATA[", i):
            end = xml.find("]]>", i + 9)
            if end < 0:
                break
            line += xml.count("\n", i, end + 3)
            i = end + 3
            continue
        if xml.startswith("<?", i):
            end = xml.find("?>", i + 2)
            if end < 0:
                break
            line += xml.count("\n", i, end + 2)
            i = end + 2
            continue
        if xml[i] != "<":
            if xml[i] == "\n":
                line += 1
            i += 1
            continue
        match = _TAG.match(xml, i)
        if not match:
            i += 1
            continue
        closing, _ns, name, _attrs, empty = match.groups()
        if closing:
            if stack:
                stack.pop()
        elif empty:
            _record(stack, name, line, hits)
        else:
            _record(stack, name, line, hits)
            stack.append(name)
        i = match.end()
    return hits


def _record(stack: list[str], name: str, line: int, hits: dict[str, int]) -> None:
    path = (*stack, name)
    if path == ("project", "version"):
        hits.setdefault("version", line)
    elif path == ("project", "artifactId"):
        hits.setdefault("artifactId", line)
    elif path == ("project", "scm", "tag"):
        hits.setdefault("scm.tag", line)


def splice(xml: str, line_no: int, tag: str, new_text: str) -> str:
    lines = xml.splitlines(keepends=True)
    if line_no < 1 or line_no > len(lines):
        raise PomError(f"line {line_no} out of range for <{tag}>")
    escaped = re.escape(tag)
    compiled = re.compile(
        rf"(<(?:[\w.-]+:)?{escaped}(?:\s[^>]*)?>)(.*?)(</(?:[\w.-]+:)?{escaped}>)"
    )
    line = lines[line_no - 1]
    updated, n = compiled.subn(rf"\g<1>{new_text}\g<3>", line, count=1)
    if n != 1:
        raise PomError(f"could not splice <{tag}> on line {line_no}: {line.rstrip()!r}")
    lines[line_no - 1] = updated
    return "".join(lines)


def apply_version(xml: str, new_version: str, scm_tag: str | None) -> str:
    fields = read_fields(xml)
    out = splice(xml, fields.version_line, "version", new_version)
    if scm_tag is not None and fields.scm_tag_line is not None:
        # Re-read line numbers from original; version splice does not shift lines.
        out = splice(out, fields.scm_tag_line, "tag", scm_tag)
    return out
