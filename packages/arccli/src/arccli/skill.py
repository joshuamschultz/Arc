"""Skill management CLI — `arc skill` commands."""

from __future__ import annotations

import re
from pathlib import Path

import click

from arccli.formatting import click_echo, print_table

_GLOBAL_SKILL_DIR = Path.home() / ".arcagent" / "skills"

_SKILL_TEMPLATE = '''\
---
name: {name}
description: "{name} skill — edit this description"
version: "0.1.0"
author: ""
category: ""
tags: []
requires: []
---

# {name}

## Purpose

Describe what this skill does and when to use it.

## Instructions

Step-by-step instructions for the agent.

## Examples

Provide examples of input/output or usage patterns.
'''


@click.group("skill")
def skill() -> None:
    """Skill management — list, create, validate, search."""


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@skill.command("list")
@click.option("--agent", "agent_dir", default=None, help="Agent directory to include workspace skills.")
def skill_list(agent_dir: str | None) -> None:
    """List discovered skills.

    Scans ~/.arcagent/skills/ and optionally an agent's workspace/skills/.
    """
    try:
        from arcagent.core.skill_registry import SkillRegistry
        registry = SkillRegistry()
        workspace = Path(agent_dir).expanduser().resolve() if agent_dir else Path("/nonexistent")
        skills = registry.discover(workspace, _GLOBAL_SKILL_DIR)
    except ImportError:
        skills = _discover_skills_fallback(agent_dir)

    if not skills:
        click_echo("No skills found.")
        click_echo(f"  Global dir: {_GLOBAL_SKILL_DIR}")
        if agent_dir:
            click_echo(f"  Agent dir:  {Path(agent_dir).expanduser().resolve() / 'workspace' / 'skills'}")
        return

    rows = []
    for s in skills:
        name = s.name if hasattr(s, "name") else s.get("name", "?")
        desc = s.description if hasattr(s, "description") else s.get("description", "")
        cat = s.category if hasattr(s, "category") else s.get("category", "")
        fpath = str(s.file_path) if hasattr(s, "file_path") else s.get("file_path", "")
        if len(desc) > 60:
            desc = desc[:57] + "..."
        rows.append([name, desc, cat, fpath])

    print_table(["Name", "Description", "Category", "Path"], rows)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@skill.command("create")
@click.argument("name")
@click.option("--dir", "target_dir", default=None, help="Output directory (default: cwd).")
@click.option("--global", "use_global", is_flag=True, help="Write to ~/.arcagent/skills/.")
def skill_create(name: str, target_dir: str | None, use_global: bool) -> None:
    """Scaffold a new SKILL.md with YAML frontmatter.

    Creates NAME.md with a template ready for editing.
    """
    if use_global:
        out_dir = _GLOBAL_SKILL_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
    elif target_dir:
        out_dir = Path(target_dir).expanduser().resolve()
    else:
        out_dir = Path.cwd()

    out_file = out_dir / f"{name}.md"
    if out_file.exists():
        raise click.ClickException(f"File already exists: {out_file}")

    out_file.write_text(_SKILL_TEMPLATE.format(name=name))
    click_echo(f"Created skill: {out_file}")
    click_echo()
    click_echo("Next steps:")
    click_echo(f"  1. Edit {out_file} to add skill content")
    click_echo(f"  2. arc skill validate {out_file}")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@skill.command("validate")
@click.argument("path")
def skill_validate(path: str) -> None:
    """Validate a skill file.

    Checks YAML frontmatter for required fields (name, description).
    """
    skill_path = Path(path).expanduser().resolve()
    if not skill_path.exists():
        raise click.ClickException(f"File not found: {skill_path}")

    content = skill_path.read_text(encoding="utf-8")
    frontmatter = _extract_frontmatter(content)

    if frontmatter is None:
        click_echo(f"  [FAIL] No YAML frontmatter found (expected --- delimiters)")
        raise SystemExit(1)

    parsed = _parse_yaml_simple(frontmatter)

    errors = []
    if "name" not in parsed or not parsed["name"]:
        errors.append("Missing required field: name")
    if "description" not in parsed or not parsed["description"]:
        errors.append("Missing required field: description")

    if errors:
        for e in errors:
            click_echo(f"  [FAIL] {e}")
        raise SystemExit(1)

    click_echo(f"  [OK] {skill_path.name}")
    click_echo(f"       Name:        {parsed['name']}")
    click_echo(f"       Description: {parsed['description']}")
    if parsed.get("category"):
        click_echo(f"       Category:    {parsed['category']}")
    if parsed.get("version"):
        click_echo(f"       Version:     {parsed['version']}")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@skill.command("search")
@click.argument("query")
@click.option("--agent", "agent_dir", default=None, help="Agent directory to include workspace skills.")
def skill_search(query: str, agent_dir: str | None) -> None:
    """Search skills by name or description.

    Filters discovered skills where QUERY appears in name or description
    (case-insensitive).
    """
    try:
        from arcagent.core.skill_registry import SkillRegistry
        registry = SkillRegistry()
        workspace = Path(agent_dir).expanduser().resolve() if agent_dir else Path("/nonexistent")
        skills = registry.discover(workspace, _GLOBAL_SKILL_DIR)
    except ImportError:
        skills = _discover_skills_fallback(agent_dir)

    query_lower = query.lower()
    matches = []
    for s in skills:
        name = s.name if hasattr(s, "name") else s.get("name", "")
        desc = s.description if hasattr(s, "description") else s.get("description", "")
        if query_lower in name.lower() or query_lower in desc.lower():
            cat = s.category if hasattr(s, "category") else s.get("category", "")
            fpath = str(s.file_path) if hasattr(s, "file_path") else s.get("file_path", "")
            if len(desc) > 60:
                desc = desc[:57] + "..."
            matches.append([name, desc, cat, fpath])

    if matches:
        print_table(["Name", "Description", "Category", "Path"], matches)
    else:
        click_echo(f"No skills matching '{query}'.")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _extract_frontmatter(text: str) -> str | None:
    """Extract YAML frontmatter between --- delimiters."""
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    return match.group(1) if match else None


def _parse_yaml_simple(text: str) -> dict[str, str]:
    """Minimal YAML parser for flat key: value frontmatter."""
    result: dict[str, str] = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if value in ("[]", ""):
                continue
            result[key] = value
    return result


def _discover_skills_fallback(agent_dir: str | None) -> list[dict]:
    """Fallback skill discovery when arcagent is not importable."""
    skills: list[dict] = []
    dirs_to_scan: list[Path] = []

    if _GLOBAL_SKILL_DIR.is_dir():
        dirs_to_scan.append(_GLOBAL_SKILL_DIR)

    if agent_dir:
        ws_skills = Path(agent_dir).expanduser().resolve() / "workspace" / "skills"
        if ws_skills.is_dir():
            dirs_to_scan.append(ws_skills)
            agent_created = ws_skills / "_agent-created"
            if agent_created.is_dir():
                dirs_to_scan.append(agent_created)

    for directory in dirs_to_scan:
        for md_file in sorted(directory.glob("*.md")):
            content = md_file.read_text(encoding="utf-8")
            fm = _extract_frontmatter(content)
            if fm:
                parsed = _parse_yaml_simple(fm)
                if parsed.get("name"):
                    skills.append({
                        "name": parsed.get("name", md_file.stem),
                        "description": parsed.get("description", ""),
                        "category": parsed.get("category", ""),
                        "file_path": str(md_file),
                    })

    return skills
