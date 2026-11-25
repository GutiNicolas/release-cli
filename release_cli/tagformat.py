"""Tag names of a release. `tag_format` is a list of templates with `{version}` and `{artifact}`.

The first template is the primary tag (notes, history, `connectors run`, saved run state); every
template is created and pushed together, and any of them is accepted where a tag is typed.
Pure except `problem()`, which asks `git check-ref-format` whether a sample tag is valid (skipped when git is missing).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from release_cli.versioning import PlanError, parse

DEFAULT: tuple[str, ...] = ("{version}", "{artifact}-{version}")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
# Literal text a tag may carry around the placeholders: enough for `v`, `app-`, `release/`; nothing git rejects.
_LITERAL = re.compile(r"^[A-Za-z0-9._/+-]*$")


def _git_accepts(tag: str) -> bool:
    """False when git refuses `refs/tags/<tag>` or `git tag` would read the tag as an option."""
    if tag.startswith("-"):
        return False
    try:
        done = subprocess.run(["git", "check-ref-format", f"refs/tags/{tag}"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return True  # no usable git here: the later `git tag` still refuses it
    return done.returncode == 0


def problem(formats: object) -> str | None:
    """Why `formats` is not a usable tag_format, or None when it is."""
    if not isinstance(formats, (list, tuple)) or not formats:
        return "tag_format must be a non-empty list of templates"
    seen: set[str] = set()
    for template in formats:
        if not isinstance(template, str) or not template:
            return "tag_format entries must be non-empty strings"
        if template in seen:
            return f"tag_format lists {template!r} twice"
        seen.add(template)
        names = _PLACEHOLDER.findall(template)
        if names.count("version") != 1:
            return f"tag_format entry {template!r} must contain {{version}} exactly once"
        if names.count("artifact") > 1:
            return f"tag_format entry {template!r} uses {{artifact}} more than once"
        unknown = sorted(set(names) - {"version", "artifact"})
        if unknown:
            return f"tag_format entry {template!r} has unknown placeholder {{{unknown[0]}}} (use {{version}} and {{artifact}})"
        literal = _PLACEHOLDER.sub("", template)
        if "{" in literal or "}" in literal or not _LITERAL.match(literal) or ".." in literal or "//" in literal:
            return f"tag_format entry {template!r} is not a valid tag name"
        sample = template.replace("{artifact}", "example-app").replace("{version}", "1.2.0-rc0")
        if not _git_accepts(sample):
            return f"tag_format entry {template!r} renders {sample!r}, which git cannot tag"
    return None


@dataclass(frozen=True)
class TagScheme:
    formats: tuple[str, ...] = DEFAULT
    artifact: str = ""

    def render(self, version: str) -> list[str]:
        """Every tag of `version`, primary first. `version` may carry a glob (`1.2.0-rc*`) to build ls-remote patterns."""
        return [t.replace("{artifact}", self.artifact).replace("{version}", version) for t in self.formats]

    def primary(self, version: str) -> str:
        return self.render(version)[0]

    def _match(self, tag: str, formats: tuple[str, ...]) -> str | None:
        for template in formats:
            pattern = re.escape(template).replace(re.escape("{artifact}"), re.escape(self.artifact)).replace(re.escape("{version}"), "(?P<v>.+)")
            found = re.fullmatch(pattern, tag)
            if found is None:
                continue
            try:
                parse(found["v"])
            except PlanError:
                continue
            return found["v"]
        return None

    def version_of(self, tag: str) -> str | None:
        """The version a tag in any configured form names (a SNAPSHOT parses too), or None."""
        return self._match(tag, self.formats)

    def primary_version_of(self, tag: str) -> str | None:
        """Same, for the primary form only: what history, previous-tag and latest-tag walk."""
        return self._match(tag, self.formats[:1])


def scheme_of(context: dict) -> TagScheme:
    """The scheme a connector request carries (older requests carry none: the default)."""
    return TagScheme(tuple(context.get("tag_format") or DEFAULT), context.get("artifact", ""))


def tag_of(context: dict) -> str:
    """The primary tag of a connector request (older requests only had release_version, which was it)."""
    return context.get("tag") or context["release_version"]
