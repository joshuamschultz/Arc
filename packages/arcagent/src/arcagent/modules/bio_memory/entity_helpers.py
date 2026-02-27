"""Shared entity file operations for bio-memory consolidators.

DRY extraction: methods used by both Consolidator and DeepConsolidator
for entity path resolution, validation, frontmatter updates, and linking.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from arcagent.utils.io import atomic_write_text
from arcagent.utils.sanitizer import read_frontmatter, sanitize_wiki_link

# Wiki-link pattern — single definition for the whole module
WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def today_str() -> str:
    """Return today's date as YYYY-MM-DD in UTC."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def resolve_entity_path(
    slug: str,
    entities_dir: Path,
    workspace: Path,
) -> Path | None:
    """Resolve entity slug to file path. Checks entities_dir and subdirs.

    Returns None if not found or path escapes workspace bounds.
    """
    if not entities_dir.exists():
        return None

    # Direct match
    candidate = entities_dir / f"{slug}.md"
    if candidate.exists():
        return validate_entity_path(candidate, workspace)

    # Subdirectory match
    for sub_candidate in entities_dir.rglob(f"{slug}.md"):
        return validate_entity_path(sub_candidate, workspace)

    return None


def validate_entity_path(path: Path, workspace: Path) -> Path | None:
    """Validate path is within workspace bounds."""
    try:
        path.resolve().relative_to(workspace.resolve())
        return path
    except ValueError:
        return None


def update_frontmatter_field(text: str, field: str, value: Any) -> str:
    """Update a single field in YAML frontmatter without full re-parse."""
    if not text.startswith("---"):
        return text

    end = text.find("\n---", 3)
    if end == -1:
        return text

    fm_text = text[4:end]
    body = text[end + 4 :]

    try:
        fm = yaml.safe_load(fm_text)
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        return text

    fm[field] = value
    new_fm = yaml.dump(fm, default_flow_style=False, sort_keys=False).strip()
    return f"---\n{new_fm}\n---{body}"


def append_to_section(text: str, section_header: str, line: str) -> str:
    """Append a line to a markdown section. Creates section if missing."""
    if section_header not in text:
        text = text.rstrip("\n") + f"\n\n{section_header}\n{line}"
        return text

    idx = text.index(section_header)
    after = idx + len(section_header) + 1  # skip header + newline

    # Find next ## section
    next_section = text.find("\n## ", after)
    if next_section == -1:
        text = text.rstrip("\n") + f"\n{line}"
    else:
        text = text[:next_section] + line + text[next_section:]

    return text


def add_link_to_frontmatter(
    entity_path: Path,
    target_slug: str,
) -> bool:
    """Add [[target_slug]] to entity's links_to if not already present."""
    fm = read_frontmatter(entity_path)
    if fm is None:
        return False

    links_to = fm.get("links_to", [])
    if not isinstance(links_to, list):
        links_to = []

    link_ref = f"[[{target_slug}]]"
    if link_ref in links_to or target_slug in links_to:
        return False

    links_to.append(link_ref)

    text = entity_path.read_text(encoding="utf-8")
    end = text.find("\n---", 3)
    if end == -1:
        return False
    fm["links_to"] = links_to
    fm_text = yaml.dump(fm, default_flow_style=False, sort_keys=False).strip()
    body = text[end + 4 :]
    atomic_write_text(entity_path, f"---\n{fm_text}\n---{body}")
    return True


def normalize_entity_file(
    path: Path,
    entities_dir: Path,
) -> None:
    """Ensure entity file has v2.1 YAML frontmatter.

    Lazy normalization: legacy LLM-created files without frontmatter
    get v2.1 frontmatter added on first touch.
    """
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        return  # Already has frontmatter

    is_subdirectory = path.parent != entities_dir
    entity_type = path.parent.name.rstrip("s") if is_subdirectory else "unknown"
    entity_id = path.stem
    name = entity_id.replace("-", " ").title()
    for line in text.split("\n"):
        if line.startswith("# "):
            name = line[2:].strip()
            break

    date = today_str()
    frontmatter = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "name": name,
        "status": "active",
        "last_updated": date,
        "last_verified": date,
        "created": date,
        "links_to": [],
        "tags": [],
        "classification": "unclassified",
    }
    fm_text = yaml.dump(
        frontmatter,
        default_flow_style=False,
        sort_keys=False,
    ).strip()

    new_text = f"---\n{fm_text}\n---\n\n{text}"
    atomic_write_text(path, new_text)


def extract_wiki_links(text: str) -> list[str]:
    """Extract all [[wiki-link]] slugs from text, sanitized."""
    slugs: list[str] = []
    for match in WIKI_LINK_RE.finditer(text):
        slug = sanitize_wiki_link(match.group(1))
        if slug:
            slugs.append(slug)
    return slugs


class EntityIndex:
    """Lazy slug-to-path mapping. Single rglob scan, O(1) lookups after.

    Replaces per-call rglob scans that become expensive at scale.
    Create one per operation (search, consolidation cycle), reuse for
    all lookups within that operation. Call refresh() after mutations.
    """

    def __init__(self, entities_dir: Path, workspace: Path) -> None:
        self._entities_dir = entities_dir
        self._workspace = workspace
        self._cache: dict[str, Path] | None = None

    def _build(self) -> dict[str, Path]:
        """Scan entities directory once, build slug→path mapping."""
        if not self._entities_dir.exists():
            return {}
        return {p.stem: p for p in self._entities_dir.rglob("*.md")}

    @property
    def _index(self) -> dict[str, Path]:
        if self._cache is None:
            self._cache = self._build()
        return self._cache

    def resolve(self, slug: str) -> Path | None:
        """Resolve entity slug to file path. O(1) after first call."""
        path = self._index.get(slug)
        if path is None:
            return None
        return validate_entity_path(path, self._workspace)

    def all_files(self) -> list[Path]:
        """Return all entity file paths."""
        return list(self._index.values())

    def refresh(self) -> None:
        """Invalidate cache. Next access rebuilds from filesystem."""
        self._cache = None
