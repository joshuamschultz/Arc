"""Workspace hygiene — non-lossy merge of duplicate memory cards + backlink repair.

All memory-maintenance logic lives here (not in arccli), so a deployment can swap
arcmemory out wholesale. Two families of routine live here:

* **dedup** — collapse pre-canonicalization duplicate cards. The distiller once wrote
  entity/procedure/insight cards under free-text slugs, so one real thing became
  several files ("Custom ERP.md", "custom-erp.md", "custom_erp.md"). After slug
  canonicalization the store reads a card only by its canonical stem, orphaning the
  variants. ``dedup_workspace`` groups the variant files by canonical slug, unions
  their content (facts/cues/instances unioned, richest metadata wins,
  confidence/use-count combined), and — when ``apply`` — writes the single canonical
  file and deletes the variants. Files are read RAW (not through the store, whose read
  path canonicalizes and would skip a legacy stem). Idempotent.

* **backlink repair** — a ``[[dst]]`` wiki-link writes ``links_to`` only in the SOURCE
  card. ``repair_backlinks`` walks every card and writes the reciprocal link into the
  TARGET card so the relationship is navigable from both ends (and the graph edge
  exists both ways). Idempotent: a second pass changes nothing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from arcmemory.mdfile import atomic_write_text, parse_document, render_document
from arcmemory.security import dominating_classification
from arcmemory.slug import canonical_slug
from arcmemory.stores.semantic import (
    SemanticStore,
    extract_wiki_links,
    format_fact,
    parse_facts,
)
from arcmemory.types import Confidence, Fact, Insight, Procedure

_STORES = ("entities", "procedures", "insights")


# ---------------------------------------------------------------------------
# reporting types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupMerge:
    """One canonical slug and the variant files that collapse onto it."""

    canonical: str
    sources: list[str]  # file names in the group (>1)
    deleted: int  # variants removed (sources minus the canonical target)


@dataclass(frozen=True)
class StoreReport:
    """Per-store (entities/procedures/insights) dedup outcome for one workspace."""

    store: str
    merges: list[GroupMerge]

    @property
    def files_deleted(self) -> int:
        return sum(m.deleted for m in self.merges)


@dataclass(frozen=True)
class DedupReport:
    """Everything dedup did (or would do) for one workspace."""

    workspace: Path
    stores: list[StoreReport]

    @property
    def groups(self) -> int:
        return sum(len(s.merges) for s in self.stores)


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------


def _groups(directory: Path) -> dict[str, list[Path]]:
    """Map canonical slug -> the ``.md`` files that collapse onto it (dupes only)."""
    by_canon: dict[str, list[Path]] = defaultdict(list)
    if directory.exists():
        for path in sorted(directory.glob("*.md")):
            by_canon[canonical_slug(path.stem)].append(path)
    return {canon: paths for canon, paths in by_canon.items() if len(paths) > 1}


# ---------------------------------------------------------------------------
# per-store doc builders (union facts/cues/instances, richest metadata wins)
# ---------------------------------------------------------------------------


def _build_entity_doc(canonical: str, paths: list[Path]) -> str:
    """Merge entity cards: highest-confidence fact per predicate, richest metadata."""
    facts_by_pred: dict[str, Fact] = {}
    name = ""
    entity_type = ""
    classifications: list[str] = []
    links: list[str] = []
    tags: list[str] = []
    for path in paths:
        fm, body = parse_document(path.read_text(encoding="utf-8"))
        for fact in parse_facts(body):
            prior = facts_by_pred.get(fact.predicate)
            if prior is None or fact.confidence > prior.confidence:
                facts_by_pred[fact.predicate] = fact
        cand_name = str(fm.get("name", "")).strip()
        if len(cand_name) > len(name):
            name = cand_name
        if str(fm.get("entity_type", "unknown")) != "unknown":
            entity_type = str(fm.get("entity_type"))
        classifications.append(str(fm.get("classification", "unclassified")))
        links += [str(x) for x in fm.get("links_to", [])]
        tags += [str(x) for x in fm.get("tags", [])]

    name = name or canonical.replace("-", " ").title()
    frontmatter = {
        "entity_type": entity_type or "unknown",
        "entity_id": canonical,
        "name": name,
        "classification": dominating_classification(classifications),
        "cross_session_visibility": False,
        "confidence": max((f.confidence for f in facts_by_pred.values()), default=0.5),
        "links_to": list(dict.fromkeys(links)),
        "tags": list(dict.fromkeys(tags)),
    }
    fact_lines = "\n".join(format_fact(facts_by_pred[p]) for p in sorted(facts_by_pred))
    return render_document(frontmatter, f"# {name}\n\n## Facts\n{fact_lines}")


def _read_procedure_raw(path: Path) -> Procedure:
    """Parse a procedure card from its actual file (not via the canonicalizing store)."""
    fm, body = parse_document(path.read_text(encoding="utf-8"))
    steps = [
        line.split(". ", 1)[1].strip()
        for line in body.splitlines()
        if line.strip() and line.strip()[0].isdigit() and ". " in line
    ]
    return Procedure(
        slug=str(fm.get("slug", path.stem)),
        title=str(fm.get("title", path.stem)),
        when_to_use=str(fm.get("when_to_use", "")),
        steps=steps,
        use_count=int(fm.get("use_count", 0)),
        classification=str(fm.get("classification", "unclassified")),
    )


def _build_procedure_doc(canonical: str, paths: list[Path]) -> str:
    """Merge procedure cards: sum use_count, keep the richest step list."""
    cards = [_read_procedure_raw(p) for p in paths]
    richest = max(cards, key=lambda c: len(c.steps))
    card = Procedure(
        slug=canonical,
        title=richest.title,
        when_to_use=richest.when_to_use,
        steps=richest.steps,
        use_count=sum(c.use_count for c in cards),
        classification=dominating_classification([c.classification for c in cards]),
    )
    frontmatter = {
        "slug": card.slug,
        "title": card.title,
        "when_to_use": card.when_to_use,
        "use_count": card.use_count,
        "classification": card.classification,
    }
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(card.steps, start=1))
    when = f"## When to use\n{card.when_to_use}\n\n" if card.when_to_use else ""
    return render_document(frontmatter, f"# {card.title}\n\n{when}## Steps\n{steps}")


def _read_insight_raw(path: Path) -> Insight:
    """Parse an insight card from its actual file (not via the canonicalizing store)."""
    fm, body = parse_document(path.read_text(encoding="utf-8"))
    statement = body.split("## Statement", 1)[1].strip() if "## Statement" in body else ""
    return Insight(
        id=str(fm.get("id", path.stem)),
        statement=statement,
        trigger=str(fm.get("trigger", "")),
        cues=[str(x) for x in fm.get("cues", [])],
        instances=[str(x) for x in fm.get("instances", [])],
        classification=str(fm.get("classification", "unclassified")),
        confidence=float(fm.get("confidence", 0.0)),
        salience=float(fm.get("salience", 0.0)),
        status=Confidence(str(fm.get("status", "guessed"))),
        hits=int(fm.get("hits", 0)),
    )


def _build_insight_doc(canonical: str, paths: list[Path]) -> str:
    """Merge insight cards: union cues/instances, max confidence/salience/hits."""
    cards = [_read_insight_raw(p) for p in paths]
    best = max(cards, key=lambda c: c.hits)
    cues: list[str] = []
    instances: list[str] = []
    for c in cards:
        cues += c.cues
        instances += c.instances
    card = Insight(
        id=canonical,
        statement=best.statement,
        trigger=best.trigger,
        cues=list(dict.fromkeys(cues)),
        instances=list(dict.fromkeys(instances)),
        classification=dominating_classification([c.classification for c in cards]),
        confidence=max(c.confidence for c in cards),
        salience=max(c.salience for c in cards),
        status=max(cards, key=lambda c: c.confidence).status,
        hits=max(c.hits for c in cards),
    )
    frontmatter = {
        "id": card.id,
        "trigger": card.trigger,
        "cues": card.cues,
        "instances": card.instances,
        "classification": card.classification,
        "confidence": card.confidence,
        "salience": card.salience,
        "status": card.status.value,
        "hits": card.hits,
    }
    return render_document(frontmatter, f"# {card.id}\n\n## Statement\n{card.statement}")


_BUILDERS = {
    "entities": _build_entity_doc,
    "procedures": _build_procedure_doc,
    "insights": _build_insight_doc,
}


# ---------------------------------------------------------------------------
# dedup driver
# ---------------------------------------------------------------------------


def _dedup_store(mem_dir: Path, store: str, *, apply: bool) -> StoreReport:
    """Merge every duplicate group in one store; write + delete when ``apply``."""
    directory = mem_dir / store
    build = _BUILDERS[store]
    merges: list[GroupMerge] = []
    for canonical, paths in _groups(directory).items():
        target = directory / f"{canonical}.md"
        variants = [p for p in paths if p != target]
        if apply:
            atomic_write_text(target, build(canonical, paths))
            for path in variants:
                path.unlink(missing_ok=True)
        merges.append(
            GroupMerge(canonical=canonical, sources=[p.name for p in paths], deleted=len(variants))
        )
    return StoreReport(store=store, merges=merges)


def dedup_workspace(workspace: Path, *, apply: bool) -> DedupReport:
    """Dedup all three stores of one workspace's ``memory/`` directory (dry-run default)."""
    mem_dir = Path(workspace) / "memory"
    stores = [_dedup_store(mem_dir, store, apply=apply) for store in _STORES]
    return DedupReport(workspace=Path(workspace), stores=stores)


def discover_workspaces(root: Path) -> list[Path]:
    """Return the workspace dir(s) reachable from ``root``.

    A workspace is a dir with ``memory/entities/`` under it. If ``root`` is itself a
    workspace it is used directly; otherwise ``root`` is treated as a parent and
    searched recursively for nested workspaces (a fleet dir).
    """
    root = Path(root)
    if (root / "memory" / "entities").is_dir():
        return [root]
    found = {mem.parent for mem in root.rglob("memory") if (mem / "entities").is_dir()}
    return sorted(found)


# ---------------------------------------------------------------------------
# backlink repair (bidirectional links)
# ---------------------------------------------------------------------------


def repair_backlinks(store: SemanticStore) -> int:
    """Write the reciprocal backlink into every wiki-link target card. Idempotent.

    For each entity S linking to a real card T (``[[T]]`` in ``links_to``), ensure T
    lists S in its own ``links_to`` (and the graph carries the T->S edge). Running a
    second time is a no-op — ``add_link`` skips a link the target already records.
    Returns the number of reciprocal backlinks newly written.
    """
    written = 0
    for slug in store.slugs():
        entity = store.read(slug)
        if entity is None:
            continue
        for ref in entity.links_to:
            for target in extract_wiki_links(ref) or [ref]:
                target_slug = canonical_slug(target)
                if target_slug == slug or store.read(target_slug) is None:
                    continue
                if store.add_link(target_slug, slug):
                    written += 1
    return written


__all__ = [
    "DedupReport",
    "GroupMerge",
    "StoreReport",
    "dedup_workspace",
    "discover_workspaces",
    "repair_backlinks",
]
