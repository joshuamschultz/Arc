"""Plain CommandDef handlers for the `arc skill` subcommand group.

T1.1.5 migration: replaces the legacy Click-based dispatch in registry.py.
Each function is a direct translation of the corresponding Click command body
in arccli.skill, with Click-specific calls replaced with stdlib equivalents.

Layer contract: this module may import from arcagent.
It MUST NOT import click or arccli.main_legacy.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

_GLOBAL_SKILL_DIR = Path.home() / ".arcagent" / "skills"

_SKILL_TEMPLATE = """\
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
"""


# ---------------------------------------------------------------------------
# Internal helpers (no click dependency)
# ---------------------------------------------------------------------------


def _write(msg: str = "") -> None:
    """Write a line to stdout."""
    sys.stdout.write(msg + "\n")


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a table with headers."""
    try:
        from arccli.formatting import print_table

        print_table(headers, rows)
    except ImportError:
        sys.stdout.write("  " + "  ".join(headers) + "\n")
        for row in rows:
            sys.stdout.write("  " + "  ".join(row) + "\n")


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


def _discover_skills_fallback(agent_dir: str | None) -> list[dict[str, str]]:
    """Fallback skill discovery when arcagent is not importable."""
    skills: list[dict[str, str]] = []
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
                    skills.append(
                        {
                            "name": parsed.get("name", md_file.stem),
                            "description": parsed.get("description", ""),
                            "category": parsed.get("category", ""),
                            "file_path": str(md_file),
                        }
                    )

    return skills


def _get_skills(agent_dir: str | None) -> list[Any]:
    """Discover skills from arcagent or fallback."""
    try:
        from arcagent.core.skill_registry import SkillRegistry

        registry = SkillRegistry()
        workspace = Path(agent_dir).expanduser().resolve() if agent_dir else Path("/nonexistent")
        return registry.discover(workspace, _GLOBAL_SKILL_DIR)
    except ImportError:
        return _discover_skills_fallback(agent_dir)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _list(args: argparse.Namespace) -> None:
    """List discovered skills."""
    agent_dir: str | None = getattr(args, "agent", None)
    skills = _get_skills(agent_dir)

    if not skills:
        _write("No skills found.")
        _write(f"  Global dir: {_GLOBAL_SKILL_DIR}")
        if agent_dir:
            agent_skills = Path(agent_dir).expanduser().resolve() / "workspace" / "skills"
            _write(f"  Agent dir:  {agent_skills}")
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

    _print_table(["Name", "Description", "Category", "Path"], rows)


def _create(args: argparse.Namespace) -> None:
    """Scaffold a new SKILL.md with YAML frontmatter."""
    name: str = args.name
    target_dir: str | None = getattr(args, "dir", None)
    use_global: bool = getattr(args, "use_global", False)

    if use_global:
        out_dir = _GLOBAL_SKILL_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
    elif target_dir:
        out_dir = Path(target_dir).expanduser().resolve()
    else:
        out_dir = Path.cwd()

    out_file = out_dir / f"{name}.md"
    if out_file.exists():
        sys.stderr.write(f"Error: File already exists: {out_file}\n")
        sys.exit(1)

    out_file.write_text(_SKILL_TEMPLATE.format(name=name))
    _write(f"Created skill: {out_file}")
    _write()
    _write("Next steps:")
    _write(f"  1. Edit {out_file} to add skill content")
    _write(f"  2. arc skill validate {out_file}")


def _validate(args: argparse.Namespace) -> None:
    """Validate a skill file."""
    path: str = args.path
    skill_path = Path(path).expanduser().resolve()
    if not skill_path.exists():
        sys.stderr.write(f"Error: File not found: {skill_path}\n")
        sys.exit(1)

    content = skill_path.read_text(encoding="utf-8")
    frontmatter = _extract_frontmatter(content)

    if frontmatter is None:
        _write("  [FAIL] No YAML frontmatter found (expected --- delimiters)")
        sys.exit(1)

    parsed = _parse_yaml_simple(frontmatter)

    errors = []
    if "name" not in parsed or not parsed["name"]:
        errors.append("Missing required field: name")
    if "description" not in parsed or not parsed["description"]:
        errors.append("Missing required field: description")

    if errors:
        for e in errors:
            _write(f"  [FAIL] {e}")
        sys.exit(1)

    _write(f"  [OK] {skill_path.name}")
    _write(f"       Name:        {parsed['name']}")
    _write(f"       Description: {parsed['description']}")
    if parsed.get("category"):
        _write(f"       Category:    {parsed['category']}")
    if parsed.get("version"):
        _write(f"       Version:     {parsed['version']}")


def _search(args: argparse.Namespace) -> None:
    """Search skills by name or description."""
    query: str = args.query
    agent_dir: str | None = getattr(args, "agent", None)
    skills = _get_skills(agent_dir)

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
        _print_table(["Name", "Description", "Category", "Path"], matches)
    else:
        _write(f"No skills matching '{query}'.")


# ---------------------------------------------------------------------------
# Argparse-based dispatcher
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for `arc skill <sub> [args]`."""
    parser = argparse.ArgumentParser(
        prog="arc skill",
        description="Skill management — list, create, validate, search.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    # list
    p = subs.add_parser("list", help="List discovered skills.")
    p.add_argument(
        "--agent", dest="agent", default=None, help="Agent directory to include workspace skills."
    )

    # create
    p = subs.add_parser("create", help="Scaffold a new SKILL.md with YAML frontmatter.")
    p.add_argument("name", help="Skill name.")
    p.add_argument("--dir", dest="dir", default=None, help="Output directory (default: cwd).")
    p.add_argument(
        "--global", dest="use_global", action="store_true", help="Write to ~/.arcagent/skills/."
    )

    # validate
    p = subs.add_parser("validate", help="Validate a skill file.")
    p.add_argument("path", help="Path to the skill .md file.")

    # search
    p = subs.add_parser("search", help="Search skills by name or description.")
    p.add_argument("query", help="Search query.")
    p.add_argument(
        "--agent", dest="agent", default=None, help="Agent directory to include workspace skills."
    )

    return parser


_SUBCOMMAND_MAP = {
    "list": _list,
    "create": _create,
    "validate": _validate,
    "search": _search,
}


def skill_handler(args: list[str]) -> None:
    """Top-level handler for `arc skill <sub> [args]`.

    Called by arccli.commands.registry when the user runs `arc skill ...`.
    """
    parser = _build_parser()

    if not args:
        parser.print_help()
        sys.exit(0)

    parsed = parser.parse_args(args)

    if parsed.subcmd is None:
        parser.print_help()
        sys.exit(0)

    fn = _SUBCOMMAND_MAP.get(parsed.subcmd)
    if fn is None:
        sys.stderr.write(f"arc skill: unknown subcommand '{parsed.subcmd}'\n")
        sys.exit(1)

    fn(parsed)
