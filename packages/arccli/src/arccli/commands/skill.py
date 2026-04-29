"""`arc skill` — manage SKILL.md folders (SPEC-021).

A skill is a folder containing `SKILL.md` (frontmatter + 7 required
sections) plus optional `references/`, `scripts/`, `templates/`, and
`assets/` sub-folders. The unified `CapabilityLoader` discovers them
from the same four scan roots used for capability `.py` files.

Layer contract: this module may import from arcagent. It MUST NOT
import click or arccli.main_legacy.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

_GLOBAL_CAP_DIR = Path.home() / ".arc" / "capabilities"

_SKILL_TEMPLATE = """\
---
name: {name}
version: 1.0.0
description: <one sentence — what this skill teaches>
triggers: [<trigger phrase 1>, <trigger phrase 2>, <trigger phrase 3>]
tools: [<tool 1>, <tool 2>]
---

## Resources

(auto-filled by the loader)

## Contract

Inputs you must have:
- <input>

Outputs the agent must produce:
- <output>

## Knowledge

<background and rationale — why this approach, what constraints>

## Steps

1. <first action>
2. <second action>
3. <third action>

## Anti Patterns

- **Don't** <specific failure mode>.
- **Don't** <another specific failure mode>.

## Examples

```python
<concrete tool invocation example>
```

## Validation

- <observable check 1>
- <observable check 2>
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write(msg: str = "") -> None:
    sys.stdout.write(msg + "\n")


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    try:
        from arccli.formatting import print_table

        print_table(headers, rows)
    except ImportError:
        sys.stdout.write("  " + "  ".join(headers) + "\n")
        for row in rows:
            sys.stdout.write("  " + "  ".join(row) + "\n")


def _scan_roots(agent_dir: str | None) -> list[tuple[str, Path]]:
    """Return user-visible scan roots in precedence order."""
    roots: list[tuple[str, Path]] = []
    if _GLOBAL_CAP_DIR.is_dir():
        roots.append(("global", _GLOBAL_CAP_DIR))
    if agent_dir:
        agent_root = Path(agent_dir).expanduser().resolve()
        agent_caps = agent_root / "capabilities"
        ws_caps = agent_root / "workspace" / ".capabilities"
        if agent_caps.is_dir():
            roots.append(("agent", agent_caps))
        if ws_caps.is_dir():
            roots.append(("workspace", ws_caps))
    return roots


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _extract_frontmatter(text: str) -> str | None:
    match = _FRONTMATTER_RE.match(text)
    return match.group(1) if match else None


def _parse_yaml_simple(text: str) -> dict[str, str]:
    """Minimal YAML parser for flat key: value frontmatter (fallback only)."""
    result: dict[str, str] = {}
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value in ("[]", ""):
            continue
        result[key] = value
    return result


def _discover_skills_fallback(agent_dir: str | None) -> list[dict[str, str]]:
    """Discover skill folders without importing arcagent. Reads SKILL.md only."""
    skills: list[dict[str, str]] = []
    for source, root in _scan_roots(agent_dir):
        for entry in sorted(root.iterdir()):
            skill_md = entry / "SKILL.md"
            if not entry.is_dir() or not skill_md.exists():
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")
            except OSError:
                # Unreadable SKILL.md — skip silently in fallback discovery.
                continue
            fm_text = _extract_frontmatter(content)
            if not fm_text:
                continue
            parsed = _parse_yaml_simple(fm_text)
            if not parsed.get("name"):
                continue
            skills.append(
                {
                    "name": parsed.get("name", entry.name),
                    "version": parsed.get("version", ""),
                    "description": parsed.get("description", ""),
                    "source": source,
                    "file_path": str(skill_md),
                }
            )
    return skills


def _get_skills(agent_dir: str | None) -> list[Any]:
    """Discover skills via arcagent's validator when available."""
    try:
        from arcagent.core.skill_validator import validate_skill_folder
    except ImportError:
        return _discover_skills_fallback(agent_dir)

    out: list[dict[str, str]] = []
    for source, root in _scan_roots(agent_dir):
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or not (entry / "SKILL.md").exists():
                continue
            result = validate_skill_folder(entry, source)
            if result.entry is None:
                continue
            out.append(
                {
                    "name": result.entry.name,
                    "version": result.entry.version,
                    "description": result.entry.description,
                    "source": source,
                    "file_path": str(result.entry.location),
                }
            )
    return out


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _list(args: argparse.Namespace) -> None:
    """List discovered skill folders."""
    agent_dir: str | None = getattr(args, "agent", None)
    skills = _get_skills(agent_dir)

    if not skills:
        _write("No skills found.")
        for source, root in _scan_roots(agent_dir):
            _write(f"  {source}: {root}")
        return

    rows = []
    for s in skills:
        name = s["name"] if isinstance(s, dict) else getattr(s, "name", "?")
        version = s["version"] if isinstance(s, dict) else getattr(s, "version", "")
        desc = (
            s["description"] if isinstance(s, dict) else getattr(s, "description", "")
        )
        source = s["source"] if isinstance(s, dict) else getattr(s, "scan_root", "")
        fpath = s["file_path"] if isinstance(s, dict) else str(getattr(s, "location", ""))
        if len(desc) > 50:
            desc = desc[:47] + "..."
        rows.append([name, version, source, desc, fpath])

    _print_table(["Name", "Version", "Source", "Description", "Path"], rows)


def _create(args: argparse.Namespace) -> None:
    """Scaffold a new SPEC-021 skill folder with SKILL.md + sub-folders."""
    name: str = args.name
    target_dir: str | None = getattr(args, "dir", None)
    use_global: bool = getattr(args, "use_global", False)

    if use_global:
        out_root = _GLOBAL_CAP_DIR
        out_root.mkdir(parents=True, exist_ok=True)
    elif target_dir:
        out_root = Path(target_dir).expanduser().resolve()
    else:
        out_root = Path.cwd()

    skill_folder = out_root / name
    if skill_folder.exists():
        sys.stderr.write(f"Error: Folder already exists: {skill_folder}\n")
        sys.exit(1)

    skill_folder.mkdir(parents=True)
    (skill_folder / "references").mkdir()
    (skill_folder / "scripts").mkdir()
    (skill_folder / "templates").mkdir()

    skill_md = skill_folder / "SKILL.md"
    skill_md.write_text(_SKILL_TEMPLATE.format(name=name))

    _write(f"Created skill: {skill_folder}/")
    _write("  SKILL.md, references/, scripts/, templates/")
    _write()
    _write("Next steps:")
    _write(f"  1. Edit {skill_md} (fill description, triggers, tools, all 7 sections)")
    _write(f"  2. arc skill validate {skill_folder}")


def _validate(args: argparse.Namespace) -> None:
    """Validate a skill folder via arcagent's SPEC-021 validator."""
    path: str = args.path
    target = Path(path).expanduser().resolve()

    # Accept either the folder or the SKILL.md inside it.
    if target.is_file() and target.name == "SKILL.md":
        folder = target.parent
    else:
        folder = target

    if not folder.is_dir():
        sys.stderr.write(f"Error: Not a folder: {folder}\n")
        sys.exit(1)

    skill_md = folder / "SKILL.md"
    if not skill_md.exists():
        sys.stderr.write(f"Error: No SKILL.md inside {folder}\n")
        sys.exit(1)

    try:
        from arcagent.core.skill_validator import validate_skill_folder
    except ImportError:
        sys.stderr.write(
            "Error: arcagent is not installed; install it to validate skill folders.\n"
        )
        sys.exit(1)

    result = validate_skill_folder(folder, "agent")

    for err in result.errors:
        _write(f"  [FAIL] {err.code}: {err.detail}")
    for warn in result.warnings:
        _write(f"  [WARN] {warn.code}: {warn.detail}")

    if not result.ok or result.entry is None:
        sys.exit(1)

    entry = result.entry
    _write(f"  [OK] {folder.name}/")
    _write(f"       Name:        {entry.name}")
    _write(f"       Version:     {entry.version}")
    _write(f"       Description: {entry.description}")
    if entry.triggers:
        _write(f"       Triggers:    {', '.join(entry.triggers)}")
    if entry.tools:
        _write(f"       Tools:       {', '.join(entry.tools)}")


def _search(args: argparse.Namespace) -> None:
    """Search skills by name or description."""
    query: str = args.query
    agent_dir: str | None = getattr(args, "agent", None)
    skills = _get_skills(agent_dir)

    query_lower = query.lower()
    matches = []
    for s in skills:
        name = s["name"] if isinstance(s, dict) else getattr(s, "name", "")
        desc = s["description"] if isinstance(s, dict) else getattr(s, "description", "")
        if query_lower not in name.lower() and query_lower not in desc.lower():
            continue
        version = s["version"] if isinstance(s, dict) else getattr(s, "version", "")
        source = s["source"] if isinstance(s, dict) else getattr(s, "scan_root", "")
        fpath = s["file_path"] if isinstance(s, dict) else str(getattr(s, "location", ""))
        if len(desc) > 50:
            desc = desc[:47] + "..."
        matches.append([name, version, source, desc, fpath])

    if matches:
        _print_table(["Name", "Version", "Source", "Description", "Path"], matches)
    else:
        _write(f"No skills matching '{query}'.")


# ---------------------------------------------------------------------------
# Argparse-based dispatcher
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc skill",
        description="Skill folder management — list, create, validate, search.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    p = subs.add_parser("list", help="List discovered skill folders.")
    p.add_argument(
        "--agent", dest="agent", default=None, help="Agent directory to include per-agent roots."
    )

    p = subs.add_parser("create", help="Scaffold a new skill folder with SKILL.md.")
    p.add_argument("name", help="Skill name (used as the folder name).")
    p.add_argument("--dir", dest="dir", default=None, help="Parent directory (default: cwd).")
    p.add_argument(
        "--global",
        dest="use_global",
        action="store_true",
        help="Write under ~/.arc/capabilities/.",
    )

    p = subs.add_parser("validate", help="Validate a skill folder (or its SKILL.md).")
    p.add_argument("path", help="Path to the skill folder or its SKILL.md.")

    p = subs.add_parser("search", help="Search skills by name or description.")
    p.add_argument("query", help="Search query.")
    p.add_argument(
        "--agent", dest="agent", default=None, help="Agent directory to include per-agent roots."
    )

    return parser


_SUBCOMMAND_MAP = {
    "list": _list,
    "create": _create,
    "validate": _validate,
    "search": _search,
}


def skill_handler(args: list[str]) -> None:
    """Top-level handler for `arc skill <sub> [args]`."""
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
