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
from arcmemory.slug import canonical_slug
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


def _fold_fact(existing: Fact | None, incoming: Fact) -> Fact:
    """Merge two facts for one predicate, keeping the higher-confidence value current.

    No prior value -> take the incoming fact verbatim. Same value -> keep the more
    corroborated one. Differing values -> the higher-confidence value wins as current
    and the loser folds into a ``| was:`` trail (additive, never destructive — the
    entity-merge analogue of ``write_fact``'s contradiction handling).
    """
    if existing is None:
        return incoming
    if existing.value == incoming.value:
        return existing if existing.confidence >= incoming.confidence else incoming
    incoming_wins = incoming.confidence > existing.confidence
    winner, loser = (incoming, existing) if incoming_wins else (existing, incoming)
    return Fact(
        predicate=winner.predicate,
        value=winner.value,
        confidence=winner.confidence,
        date=winner.date,
        was_value=loser.value,
        was_confidence=loser.confidence,
    )


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
        """Absolute path to an entity's markdown file (slug canonicalized)."""
        return self._dir / f"{canonical_slug(slug)}.md"

    def slugs(self) -> list[str]:
        """Every entity slug currently on disk (sorted)."""
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.md"))

    def read(self, slug: str) -> Entity | None:
        """Load an entity from disk (None if it does not exist)."""
        slug = canonical_slug(slug)
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
            aliases=[str(x) for x in fm.get("aliases", [])],
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
        slug = canonical_slug(slug)
        predicate = predicate[:_MAX_FACT_TEXT]
        value = value[:_MAX_FACT_TEXT]
        entity = self.read(slug)
        if entity is None:
            entity = Entity(
                slug=slug,
                name=name or slug.replace("-", " ").title(),
                entity_type=entity_type,
                classification=classification,
            )
        else:
            # Enrich the existing card in place — a later run may name or classify
            # it more precisely, but a bare "unknown" never overwrites a known type.
            if name:
                entity.name = name
            if entity_type != "unknown":
                entity.entity_type = entity_type

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

    def merge_into(self, canonical_slug_: str, other_slug: str) -> bool:
        """Fold the ``other`` entity card into ``canonical`` and delete ``other``'s file.

        The de-dup primitive behind the slow-path entity merge (mirrors cue-merge for
        entities). Non-destructive: every fact survives — a predicate the canonical
        lacks is copied over, a contradiction folds the lower-confidence value into a
        ``| was:`` trail, and the losing card's name/slug is recorded in ``aliases`` so
        the fold is inspectable and reversible from the audit chain. Links union;
        a known ``entity_type`` fills an ``unknown`` one. Returns False when either
        card is missing or the two resolve to the same slug (nothing to merge).
        """
        canonical = canonical_slug(canonical_slug_)
        other = canonical_slug(other_slug)
        if canonical == other:
            return False
        dst = self.read(canonical)
        src = self.read(other)
        if dst is None or src is None:
            return False

        by_predicate = {f.predicate: f for f in dst.facts}
        for fact in src.facts:
            existing = by_predicate.get(fact.predicate)
            by_predicate[fact.predicate] = _fold_fact(existing, fact)
        dst.facts = [by_predicate[p] for p in sorted(by_predicate)]

        for link in src.links_to:
            if link not in dst.links_to:
                dst.links_to.append(link)
        dst.aliases = sorted(
            set(dst.aliases) | set(src.aliases) | {src.name, src.slug} - {dst.name, dst.slug}
        )
        if dst.entity_type == "unknown" and src.entity_type != "unknown":
            dst.entity_type = src.entity_type

        self._persist(dst)
        self.path_for(other).unlink(missing_ok=True)
        return True

    def add_link(self, src_slug: str, dst_slug: str) -> bool:
        """Create a directed wiki-link edge and record it in ``src``'s frontmatter.

        Returns True when a new frontmatter link was written (False when the source
        card is missing or already records the link) — so backlink repair can count
        only real changes and stay idempotent.
        """
        self._graph.link(self._scope, src_slug, dst_slug, kind="link")
        entity = self.read(src_slug)
        if entity is None:
            return False
        ref = f"[[{dst_slug}]]"
        if ref in entity.links_to or dst_slug in entity.links_to:
            return False
        entity.links_to.append(ref)
        self._persist(entity)
        return True

    def aliases_index(self) -> dict[str, str]:
        """Map ``canonical(alias) -> owning entity slug`` across every card's aliases.

        The reverse index that closes the re-duplication loop: ``merge_into`` records a
        folded card's name/slug in the survivor's ``aliases``, and :meth:`resolve`
        consults this index so a later write of the aliased identity lands on the
        survivor instead of minting the duplicate anew.
        """
        index: dict[str, str] = {}
        for slug in self.slugs():
            entity = self.read(slug)
            if entity is None:
                continue
            for alias in entity.aliases:
                index[canonical_slug(alias)] = slug
        return index

    def resolve(self, slug: str, name: str = "") -> str:
        """Deterministic identity resolution: exact file, then alias, else the raw slug.

        The embedder-independent core of search-before-write: an exact canonical-slug
        file hit wins; failing that, a slug/name matching a recorded alias resolves to
        that card's canonical slug (closing the re-dup loop); otherwise the canonical
        slug is returned unchanged (a genuinely new entity). Never raises.
        """
        canonical = canonical_slug(slug)
        if self.path_for(canonical).exists():
            return canonical
        index = self.aliases_index()
        keys = [canonical] + ([canonical_slug(name)] if name.strip() else [])
        for key in keys:
            if key in index:
                return index[key]
        return canonical

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
            "aliases": entity.aliases,
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
