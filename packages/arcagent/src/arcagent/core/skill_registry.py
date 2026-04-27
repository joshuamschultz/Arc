"""Skill Registry — discover, cache, and format SKILL.md files.

Skills use progressive disclosure: only name + description are
injected into the system prompt. Full content is loaded on demand
via the read tool when the agent needs it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

import yaml

_logger = logging.getLogger("arcagent.skill_registry")


@dataclass
class SkillMeta:
    """Parsed SKILL.md frontmatter — lightweight for prompt injection."""

    name: str
    description: str
    version: str = ""
    author: str = ""
    requires: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    category: str = ""
    file_path: Path = field(default_factory=lambda: Path())


class SkillRegistry:
    """Discover, cache, and format SKILL.md files for prompt injection."""

    def __init__(self, ui_reporter: Any | None = None) -> None:
        self._skills: dict[str, SkillMeta] = {}
        # Duck-typed UIEventReporter; no arcui import. None = disabled.
        self._ui_reporter: Any | None = ui_reporter

    @property
    def skills(self) -> list[SkillMeta]:
        """All discovered skills."""
        return list(self._skills.values())

    def discover(self, workspace: Path, global_dir: Path) -> list[SkillMeta]:
        """Scan directories for SKILL.md files and cache results.

        Discovery order:
        1. workspace/skills/*.md
        2. workspace/skills/_agent-created/*.md
        3. global_dir/*.md
        """
        scan_dirs: list[Path] = []

        ws_skills = workspace / "skills"
        if ws_skills.is_dir():
            scan_dirs.append(ws_skills)

        ws_agent_created = workspace / "skills" / "_agent-created"
        if ws_agent_created.is_dir():
            scan_dirs.append(ws_agent_created)

        if global_dir.is_dir():
            scan_dirs.append(global_dir)

        for directory in scan_dirs:
            self._scan_directory(directory)

        _logger.info("Discovered %d skills", len(self._skills))
        return self.skills

    def get_skill(self, name: str) -> SkillMeta | None:
        """Look up a skill by name."""
        return self._skills.get(name)

    def format_for_prompt(self) -> str:
        """XML-formatted skill list for system prompt injection.

        Returns empty string if no skills are discovered.
        """
        if not self._skills:
            return ""

        lines = ["<available-skills>"]
        for skill in self._skills.values():
            safe_name = xml_escape(skill.name, {'"': "&quot;"})
            safe_desc = xml_escape(skill.description)
            lines.append(f'  <skill name="{safe_name}">{safe_desc}</skill>')
        lines.append("</available-skills>")
        return "\n".join(lines)

    def clear(self) -> None:
        """Clear cached skills for re-discovery."""
        self._skills.clear()

    def rescan_agent_created(self, workspace: Path) -> None:
        """Targeted re-scan of _agent-created/ directory only.

        Merges newly found skills into cache without clearing existing.
        """
        created_dir = workspace / "skills" / "_agent-created"
        if created_dir.is_dir():
            self._scan_directory(created_dir)

    def _scan_directory(self, directory: Path) -> None:
        """Scan a single directory for .md files with YAML frontmatter."""
        for path in sorted(directory.iterdir()):
            if path.suffix != ".md" or not path.is_file():
                continue
            self._parse_skill_file(path)

    def _parse_skill_file(self, path: Path) -> None:
        """Parse a single SKILL.md file and add to cache."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            _logger.warning("Cannot read skill file: %s", path)
            return

        frontmatter = self._extract_frontmatter(text)
        if frontmatter is None:
            return

        try:
            data: Any = yaml.safe_load(frontmatter)
        except yaml.YAMLError:
            _logger.warning("Invalid YAML frontmatter in: %s", path)
            return

        if not isinstance(data, dict):
            _logger.warning("Frontmatter is not a mapping in: %s", path)
            return

        name = data.get("name")
        description = data.get("description")
        if not name or not description:
            _logger.warning("Missing required fields (name, description) in: %s", path)
            return

        skill = SkillMeta(
            name=str(name),
            description=str(description),
            version=str(data.get("version", "")),
            author=str(data.get("author", "")),
            requires=data.get("requires", []) or [],
            tags=data.get("tags", []) or [],
            category=str(data.get("category", "")),
            file_path=path,
        )
        self._skills[skill.name] = skill
        self._emit_skill_load(skill)

    def _emit_skill_load(self, skill: SkillMeta) -> None:
        """Emit skill_load event to ui_reporter if registered.

        Fire-and-forget; reporter failures are logged, never propagated.
        """
        if self._ui_reporter is None:
            return
        try:
            self._ui_reporter.emit_agent_event(
                event_type="skill_load",
                data={
                    "skill_name": skill.name,
                    "version": skill.version,
                    "category": skill.category,
                },
            )
        except Exception:
            _logger.debug(
                "ui_reporter.emit_agent_event failed for skill_load", exc_info=True
            )

    @staticmethod
    def _extract_frontmatter(text: str) -> str | None:
        """Extract YAML frontmatter from between --- delimiters."""
        if not text.startswith("---"):
            return None
        end = text.find("---", 3)
        if end == -1:
            return None
        return text[3:end].strip()
