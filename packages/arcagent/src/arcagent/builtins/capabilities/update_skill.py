"""Built-in ``update_skill`` — SPEC-021 R-033.

Replaces a skill's body and bumps the frontmatter ``version`` per
``version_bump``. The frontmatter block is regenerated; sub-folders
and files (references, scripts, templates) are left untouched.
"""

from __future__ import annotations

import re

import yaml

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool

_SKILLS_SUBDIR = ".capabilities/skills"
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _bump(current: str, kind: str) -> str:
    parts = [int(p) for p in current.split(".")]
    if len(parts) != 3:
        raise ValueError(f"version {current!r} is not semver")
    major, minor, patch = parts
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"version_bump must be major/minor/patch, got {kind!r}")


@tool(
    name="update_skill",
    description=(
        "Update an existing skill's SKILL.md body in the workspace, "
        "bumping the frontmatter version."
    ),
    classification="state_modifying",
    capability_tags=["self_modification"],
    when_to_use="When you've learned a refinement worth folding into an existing skill.",
    requires_skill="update-skill",
    version="1.0.0",
)
async def update_skill(
    name: str,
    new_body: str,
    version_bump: str = "patch",
) -> str:
    """Rewrite ``<workspace>/.capabilities/skills/<name>/SKILL.md``.

    Frontmatter is preserved (with bumped ``version``); body content
    is replaced wholesale.
    """
    workspace = _runtime.workspace()
    skill_md = workspace / _SKILLS_SUBDIR / name / "SKILL.md"
    if not skill_md.exists():
        return f"Error: skill {name!r} not found"
    text = skill_md.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return f"Error: SKILL.md for {name!r} has no frontmatter block"
    fm = yaml.safe_load(match.group(1)) or {}
    current_version = str(fm.get("version", "1.0.0"))
    try:
        new_version = _bump(current_version, version_bump)
    except ValueError as exc:
        return f"Error: {exc}"
    fm["version"] = new_version
    new_frontmatter = yaml.safe_dump(fm, sort_keys=False).strip()
    skill_md.write_text(
        f"---\n{new_frontmatter}\n---\n\n{new_body}\n",
        encoding="utf-8",
    )
    return f"Updated skill {name!r} {current_version} → {new_version}"
