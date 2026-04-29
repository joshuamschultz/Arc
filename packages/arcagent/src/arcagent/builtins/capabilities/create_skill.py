"""Built-in ``create_skill`` — SPEC-021 R-032.

Scaffolds a new skill folder under
``<workspace>/.capabilities/skills/<name>/`` with the required seven
sections, four sub-folders (``references/``, ``scripts/``,
``templates/``, ``assets/``), and a frontmatter block populated from
the caller's args. The ``## Resources`` section is left blank — the
loader auto-fills it from folder contents on every reload.
"""

from __future__ import annotations

from arcagent.builtins.capabilities import _runtime
from arcagent.tools._decorator import tool

_SKILLS_SUBDIR = ".capabilities/skills"
_SUB_FOLDERS = ("references", "scripts", "templates", "assets")
_REQUIRED_SECTIONS = (
    "## Resources",
    "## Contract",
    "## Knowledge",
    "## Steps",
    "## Anti Patterns",
    "## Examples",
    "## Validation",
)


def _render_skill_md(
    *,
    name: str,
    description: str,
    triggers: list[str],
    tools: list[str],
    version: str,
    body: str,
) -> str:
    """Compose the SKILL.md content with frontmatter + 7 sections."""
    triggers_yaml = ", ".join(triggers)
    tools_yaml = ", ".join(tools)
    frontmatter = (
        "---\n"
        f"name: {name}\n"
        f"version: {version}\n"
        f"description: {description}\n"
        f"triggers: [{triggers_yaml}]\n"
        f"tools: [{tools_yaml}]\n"
        "---\n"
    )
    sections = "\n\n".join(f"{header}\n" for header in _REQUIRED_SECTIONS)
    return frontmatter + "\n" + sections + ("\n" + body if body else "")


@tool(
    name="create_skill",
    description=(
        "Scaffold a new skill folder in the workspace with frontmatter "
        "and the seven required sections. Call reload() afterwards."
    ),
    classification="state_modifying",
    capability_tags=["self_modification"],
    when_to_use=(
        "When the agent learns a procedure it should remember and "
        "structure as a skill (rather than a one-shot tool)."
    ),
    requires_skill="create-skill",
    version="1.0.0",
)
async def create_skill(
    name: str,
    description: str,
    triggers: list[str],
    tools: list[str],
    body: str = "",
    version: str = "1.0.0",
) -> str:
    """Scaffold ``workspace/.capabilities/skills/<name>/`` and return path."""
    if not name.replace("-", "_").isidentifier():
        return f"Error: name {name!r} must be alphanumeric (dashes allowed)"
    workspace = _runtime.workspace()
    folder = workspace / _SKILLS_SUBDIR / name
    if folder.exists():
        return f"Error: skill {name!r} already exists at {folder.relative_to(workspace)}"
    folder.mkdir(parents=True)
    for sub in _SUB_FOLDERS:
        (folder / sub).mkdir()
    skill_md = folder / "SKILL.md"
    skill_md.write_text(
        _render_skill_md(
            name=name,
            description=description,
            triggers=triggers,
            tools=tools,
            version=version,
            body=body,
        ),
        encoding="utf-8",
    )
    return f"Created skill {name!r} at {folder.relative_to(workspace)}"
