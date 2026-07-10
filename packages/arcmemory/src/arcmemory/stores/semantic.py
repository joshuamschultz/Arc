"""Semantic store — entities as markdown + a fact-triplet graph.

Absorbs ``bio_memory/facts.py`` (the ``predicate: value .conf date | was: prior``
triplet grammar) and ``bio_memory/entity_helpers.py`` (entity files, wiki-links,
frontmatter). Two surfaces stay in sync:

* the **markdown** entity file ``memory/entities/<slug>.md`` -- glass-box truth,
  one fact line per predicate;
* the **graph** -- a wiki-link edge per ``[[other]]`` reference, so entities form
  a traversable network for enrichment.

Contradictions are **additive**: a changed value does not erase the prior one, it
folds it into a ``| was: prior .conf`` trail (REQ-032, mem0's read-time-resolution
lesson).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from arcmemory.index.graph import WeightedGraph
from arcmemory.mdfile import atomic_write_text, parse_document, render_document
from arcmemory.types import Entity, Fact

_FACT_RE = re.compile(
    r"^-\s+(.+?):\s+(.+?)\s+(\.\d+|1)\s+(\d{4}-\d{2}-\d{2})"
    r"(?:\s+\|\s+was:\s+(.+?)\s+(\.\d+|1))?$"
)
_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_MAX_FACT_TEXT = 500


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _format_confidence(confidence: float) -> str:
    """Compact confidence: 0.9 -> '.9', 0.85 -> '.85', 1.0 -> '1'."""
    if confidence >= 1.0:
        return "1"
    return str(round(confidence, 2)).lstrip("0")


def format_fact(fact: Fact) -> str:
    """Render a ``Fact`` to its compact triplet line."""
    line = f"- {fact.predicate}: {fact.value} {_format_confidence(fact.confidence)} {fact.date}"
    if fact.was_value is not None:
        was_conf = fact.was_confidence if fact.was_confidence is not None else 0.5
        line += f" | was: {fact.was_value} {_format_confidence(was_conf)}"
    return line


def parse_fact(line: str) -> Fact | None:
    """Parse one triplet line into a ``Fact`` (None if it is not a triplet)."""
    match = _FACT_RE.match(line.strip())
    if not match:
        return None
    return Fact(
        predicate=match.group(1),
        value=match.group(2),
        confidence=float(match.group(3)),
        date=match.group(4),
        was_value=match.group(5),
        was_confidence=float(match.group(6)) if match.group(6) else None,
    )


def parse_facts(body: str) -> list[Fact]:
    """Parse every triplet line from a markdown body."""
    return [f for line in body.splitlines() if (f := parse_fact(line)) is not None]


def extract_wiki_links(text: str) -> list[str]:
    """Return the ``[[slug]]`` targets referenced in ``text``."""
    return [m.group(1).strip() for m in _WIKI_LINK_RE.finditer(text)]


class SemanticStore:
    """Read/write entity markdown + maintain the wiki-link graph for one scope."""

    def __init__(self, workspace: Path, graph: WeightedGraph, scope: str) -> None:
        self._dir = Path(workspace) / "memory" / "entities"
        self._graph = graph
        self._scope = scope

    def path_for(self, slug: str) -> Path:
        """Absolute path to an entity's markdown file."""
        return self._dir / f"{slug}.md"

    def slugs(self) -> list[str]:
        """Every entity slug currently on disk (sorted)."""
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.md"))

    def read(self, slug: str) -> Entity | None:
        """Load an entity from disk (None if it does not exist)."""
        path = self.path_for(slug)
        if not path.exists():
            return None
        fm, body = parse_document(path.read_text(encoding="utf-8"))
        return Entity(
            slug=slug,
            name=str(fm.get("name", slug.replace("-", " ").title())),
            entity_type=str(fm.get("entity_type", "unknown")),
            classification=str(fm.get("classification", "unclassified")),
            cross_session_visibility=bool(fm.get("cross_session_visibility", False)),
            confidence=float(fm.get("confidence", 0.5)),
            facts=parse_facts(body),
            links_to=[str(x) for x in fm.get("links_to", [])],
            tags=[str(x) for x in fm.get("tags", [])],
        )

    def write_fact(
        self,
        slug: str,
        predicate: str,
        value: str,
        *,
        confidence: float = 0.5,
        name: str | None = None,
        entity_type: str = "unknown",
        classification: str = "unclassified",
    ) -> Entity:
        """Add/update a fact for an entity, folding a contradiction into a ``was:`` trail."""
        predicate = predicate[:_MAX_FACT_TEXT]
        value = value[:_MAX_FACT_TEXT]
        entity = self.read(slug) or Entity(
            slug=slug,
            name=name or slug.replace("-", " ").title(),
            entity_type=entity_type,
            classification=classification,
        )

        by_predicate = {f.predicate: f for f in entity.facts}
        prior = by_predicate.get(predicate)
        was_value = was_conf = None
        if prior is not None and prior.value != value:
            was_value, was_conf = prior.value, prior.confidence
        by_predicate[predicate] = Fact(
            predicate=predicate,
            value=value,
            confidence=confidence,
            date=_today(),
            was_value=was_value,
            was_confidence=was_conf,
        )
        entity.facts = [by_predicate[p] for p in sorted(by_predicate)]

        # A [[wiki-link]] in the value becomes a graph edge (traversable enrichment)
        # and a frontmatter link — recorded in-memory so the single persist keeps both.
        for target in extract_wiki_links(value):
            self._graph.link(self._scope, slug, target, kind="link")
            ref = f"[[{target}]]"
            if ref not in entity.links_to and target not in entity.links_to:
                entity.links_to.append(ref)

        self._persist(entity)
        return entity

    def add_link(self, src_slug: str, dst_slug: str) -> None:
        """Create a directed wiki-link edge and record it in an entity's frontmatter."""
        self._graph.link(self._scope, src_slug, dst_slug, kind="link")
        entity = self.read(src_slug)
        if entity is None:
            return
        ref = f"[[{dst_slug}]]"
        if ref not in entity.links_to and dst_slug not in entity.links_to:
            entity.links_to.append(ref)
            self._persist(entity)

    def _persist(self, entity: Entity) -> None:
        """Render an entity to markdown and atomically write it."""
        frontmatter = {
            "entity_type": entity.entity_type,
            "entity_id": entity.slug,
            "name": entity.name,
            "classification": entity.classification,
            "cross_session_visibility": entity.cross_session_visibility,
            "confidence": entity.confidence,
            "last_updated": _today(),
            "links_to": entity.links_to,
            "tags": entity.tags,
        }
        fact_lines = "\n".join(format_fact(f) for f in entity.facts)
        body = f"# {entity.name}\n\n## Facts\n{fact_lines}"
        atomic_write_text(self.path_for(entity.slug), render_document(frontmatter, body))


__all__ = [
    "SemanticStore",
    "extract_wiki_links",
    "format_fact",
    "parse_fact",
    "parse_facts",
]
