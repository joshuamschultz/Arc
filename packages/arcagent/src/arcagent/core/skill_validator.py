"""SPEC-021 Task 2.8 — skill folder validator.

Parses ``SKILL.md``, validates frontmatter and section structure, and
auto-generates the ``## Resources`` section from folder contents
(R-013). Returns a :class:`SkillValidationResult` with parsed entry +
errors + warnings; the caller (loader) decides how to react per
deployment tier.

Required frontmatter fields (R-011):
  ``name``, ``description``, ``triggers``, ``tools``, ``version``

Required sections (R-012, in title-case):
  ``## Resources``, ``## Contract``, ``## Knowledge``, ``## Steps``,
  ``## Anti Patterns``, ``## Examples``, ``## Validation``

Filler detection (R-012): a section is flagged if its body matches
``"N/A"``, ``"none"``, or is empty. Filler is a warning, not an
error — federal tier may choose to block, enterprise warns,
personal logs info.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from arcagent.core.capability_registry import SkillEntry

REQUIRED_FRONTMATTER: tuple[str, ...] = (
    "name",
    "description",
    "triggers",
    "tools",
    "version",
)

REQUIRED_SECTIONS: tuple[str, ...] = (
    "## Resources",
    "## Contract",
    "## Knowledge",
    "## Steps",
    "## Anti Patterns",
    "## Examples",
    "## Validation",
)

# Sub-folders walked when generating ``## Resources``. Order matters —
# this is the order they appear in the rendered list.
_RESOURCE_FOLDERS: tuple[str, ...] = (
    "references",
    "scripts",
    "templates",
    "assets",
)

_FILLER_TOKENS: frozenset[str] = frozenset({"n/a", "none", "tbd", ""})

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_SECTION_RE = re.compile(r"^(## .+?)$", re.MULTILINE)


@dataclass(frozen=True)
class SkillValidationError:
    code: str
    detail: str


@dataclass(frozen=True)
class SkillValidationWarning:
    code: str
    detail: str


@dataclass
class SkillValidationResult:
    entry: SkillEntry | None = None
    errors: list[SkillValidationError] = field(default_factory=list)
    warnings: list[SkillValidationWarning] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_skill_folder(
    folder: Path,
    scan_root: str,
    *,
    known_tools: set[str] | None = None,
) -> SkillValidationResult:
    """Parse ``folder/SKILL.md`` and validate. Returns entry + diagnostics.

    On any error, ``result.entry`` may be ``None`` (parse failure) or
    a partial :class:`SkillEntry` (semantic failures). The caller
    should consult ``result.errors`` before registering.
    """
    skill_md = folder / "SKILL.md"
    result = SkillValidationResult()
    if not skill_md.is_file():
        result.errors.append(SkillValidationError(code="missing_skill_md", detail=str(skill_md)))
        return result

    text = skill_md.read_text(encoding="utf-8")
    parsed = _parse_skill_md(text)
    if parsed is None:
        result.errors.append(
            SkillValidationError(
                code="malformed_frontmatter",
                detail=f"{skill_md} has no leading --- frontmatter block",
            )
        )
        return result
    fm, body = parsed

    _check_required_fields(fm, result)
    _check_required_sections(body, result)
    _check_filler_sections(body, result)
    if known_tools is not None:
        _check_tool_dependencies(fm.get("tools", []), known_tools, result)

    if result.errors:
        return result

    result.entry = SkillEntry(
        name=str(fm["name"]),
        version=str(fm["version"]),
        description=str(fm["description"]),
        triggers=tuple(fm["triggers"]),
        tools=tuple(fm["tools"]),
        location=skill_md,
        scan_root=scan_root,
        model_hint=fm.get("model_hint"),
    )
    return result


def render_resources_section(folder: Path) -> str:
    """Generate ``## Resources`` body from folder contents (R-013).

    Walks the four standard sub-folders and lists their files as a
    bulleted markdown block. The loader writes this back into
    ``SKILL.md`` so the LLM always sees an accurate inventory.
    """
    lines: list[str] = ["## Resources", ""]
    any_content = False
    for sub in _RESOURCE_FOLDERS:
        sub_path = folder / sub
        if not sub_path.is_dir():
            continue
        files = sorted(p.name for p in sub_path.iterdir() if p.is_file())
        if not files:
            continue
        lines.append(f"- **{sub}/**")
        for fname in files:
            lines.append(f"  - {fname}")
        any_content = True
    if not any_content:
        lines.append("(no resources)")
    return "\n".join(lines) + "\n"


# --- Internals -------------------------------------------------------------


def _parse_skill_md(text: str) -> tuple[dict[str, Any], str] | None:
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    body = match.group(2)
    return fm, body


def _check_required_fields(fm: dict[str, Any], result: SkillValidationResult) -> None:
    missing = [k for k in REQUIRED_FRONTMATTER if k not in fm]
    if missing:
        result.errors.append(
            SkillValidationError(
                code="missing_frontmatter_field",
                detail=f"missing required fields: {missing}",
            )
        )


def _check_required_sections(body: str, result: SkillValidationResult) -> None:
    found_sections = set(_SECTION_RE.findall(body))
    missing = [section for section in REQUIRED_SECTIONS if section not in found_sections]
    if missing:
        result.errors.append(
            SkillValidationError(
                code="missing_section",
                detail=f"missing required sections: {missing}",
            )
        )


def _check_filler_sections(body: str, result: SkillValidationResult) -> None:
    """Flag sections whose body is filler ('N/A', 'none', empty)."""
    sections = _split_sections(body)
    for header, section_body in sections.items():
        if header == "## Resources":
            # Loader auto-generates this; filler here is fine.
            continue
        normalized = section_body.strip().lower()
        if normalized in _FILLER_TOKENS:
            result.warnings.append(
                SkillValidationWarning(
                    code="filler_section",
                    detail=f"{header} contains filler ({section_body.strip()!r})",
                )
            )


def _split_sections(body: str) -> dict[str, str]:
    """Return a mapping of ``## Header`` → trimmed section body."""
    out: dict[str, str] = {}
    parts = _SECTION_RE.split(body)
    if len(parts) <= 1:
        return out
    # parts: [pre_text, header1, body1, header2, body2, ...]
    for i in range(1, len(parts), 2):
        header = parts[i].strip()
        section_body = parts[i + 1] if i + 1 < len(parts) else ""
        out[header] = section_body
    return out


def _check_tool_dependencies(
    declared: Any,
    known_tools: set[str],
    result: SkillValidationResult,
) -> None:
    """Flag any tool listed in `tools:` that is not in ``known_tools``."""
    if not isinstance(declared, list):
        return
    missing = [t for t in declared if t not in known_tools]
    if missing:
        result.warnings.append(
            SkillValidationWarning(
                code="tool_dependency_policy_denied",
                detail=f"declared tools not registered: {missing}",
            )
        )


__all__ = [
    "REQUIRED_FRONTMATTER",
    "REQUIRED_SECTIONS",
    "SkillValidationError",
    "SkillValidationResult",
    "SkillValidationWarning",
    "render_resources_section",
    "validate_skill_folder",
]
