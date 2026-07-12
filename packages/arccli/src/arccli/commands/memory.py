"""``arc memory`` — operator maintenance over an agent's glass-box memory files.

Currently one subcommand:

    arc memory dedup [--apply] <workspace> [<workspace> ...]

``dedup`` merges memory cards whose slugs diverged before slug canonicalization
landed. Back then the distiller wrote entities/procedures/insights under free-text
slugs, so one real thing became several files ("Custom ERP.md", "custom_erp.md",
"custom-erp.md"). After canonicalization, the store reads a card only by its
canonical stem, so a legacy non-canonical file is now orphaned — it renders as a
duplicate and can no longer be updated in place. This command groups the variant
files by their canonical slug (using ``arcmemory.canonical_slug`` — the same
function the store uses, so the CLI and the store agree exactly), merges each
group into the single canonical-slug file, and deletes the variants. No data is
stranded: facts/cues/instances are unioned, the richest metadata wins, and
confidence/use-count are combined.

Files are read raw (NOT through the store, whose read path canonicalizes and would
skip a legacy stem). Dry-run by default; ``--apply`` writes. After applying,
restart each agent (recovery rebuilds its index) so stale surface rows drop.

A ``<workspace>`` is a directory containing ``memory/{entities,procedures,insights}/``
(on a deployed agent that is ``<agent-dir>/workspace``). If the given path is not
itself a workspace, it is treated as a root and searched for nested workspaces, so
a whole fleet dir can be cleaned in one call.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from arcmemory.mdfile import atomic_write_text, parse_document, render_document
from arcmemory.security import dominating_classification
from arcmemory.slug import canonical_slug
from arcmemory.stores.semantic import format_fact, parse_facts
from arcmemory.types import Confidence, Fact, Insight, Procedure

from arccli.commands._shared import dispatch, err
from arccli.commands._shared import write as _out

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
class WorkspaceReport:
    """Everything that dedup did (or would do) for one workspace."""

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


def _dedup_workspace(workspace: Path, *, apply: bool) -> WorkspaceReport:
    """Dedup all three stores of one workspace's ``memory/`` directory."""
    mem_dir = workspace / "memory"
    stores = [_dedup_store(mem_dir, store, apply=apply) for store in _STORES]
    return WorkspaceReport(workspace=workspace, stores=stores)


def _discover_workspaces(root: Path) -> list[Path]:
    """Return the workspace dir(s) reachable from ``root``.

    A workspace is a dir with ``memory/entities/`` under it. If ``root`` is
    itself a workspace it is used directly; otherwise ``root`` is treated as a
    parent and searched recursively for nested workspaces (a fleet dir).
    """
    if (root / "memory" / "entities").is_dir():
        return [root]
    found = {
        mem.parent for mem in root.rglob("memory") if (mem / "entities").is_dir()
    }
    return sorted(found)


# ---------------------------------------------------------------------------
# subcommand + output
# ---------------------------------------------------------------------------


def _dedup(args: argparse.Namespace) -> None:
    """Merge duplicate memory cards into their canonical-slug files.

    Dry-run by default (reports what would merge, writes nothing). ``--apply``
    performs the merge and deletes the variant files. Idempotent: a second run
    finds nothing to merge.
    """
    apply: bool = args.apply
    mode = "APPLY" if apply else "dry-run"
    total_groups = 0
    total_deleted = 0
    n_workspaces = 0

    for raw in args.workspaces:
        root = Path(raw).expanduser()
        workspaces = _discover_workspaces(root)
        if not workspaces:
            err(f"  no memory workspace found under {root}")
            continue
        for workspace in workspaces:
            n_workspaces += 1
            report = _dedup_workspace(workspace, apply=apply)
            _render_workspace(report, mode)
            total_groups += report.groups
            total_deleted += sum(s.files_deleted for s in report.stores)

    verb = "merged" if apply else "to merge"
    _out(
        f"\n{total_groups} duplicate group(s) {verb}, "
        f"{total_deleted} file(s) {'deleted' if apply else 'to delete'} "
        f"across {n_workspaces} workspace(s)."
    )
    if apply and total_groups:
        _out("Restart each affected agent so its index rebuilds and stale surface rows drop.")


def _render_workspace(report: WorkspaceReport, mode: str) -> None:
    """Print one workspace's per-store merge plan/outcome."""
    _out(f"\n{report.workspace}  ({mode})")
    if report.groups == 0:
        _out("  no duplicates.")
        return
    for store_report in report.stores:
        if not store_report.merges:
            continue
        _out(
            f"  {store_report.store}: {len(store_report.merges)} group(s), "
            f"{store_report.files_deleted} file(s) to delete"
        )
        for merge in store_report.merges:
            _out(f"    merge {merge.sources} -> {merge.canonical}.md")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc memory",
        description="Operator maintenance over an agent's glass-box memory files.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    dedup_p = subs.add_parser(
        "dedup",
        help="Merge pre-canonicalization duplicate memory cards into their canonical file.",
    )
    dedup_p.add_argument(
        "workspaces",
        nargs="+",
        metavar="<workspace>",
        help="Dir containing memory/ (or a root to search for nested workspaces).",
    )
    dedup_p.add_argument(
        "--apply",
        action="store_true",
        help="Perform the merge and delete variants (default: dry-run).",
    )
    return parser


_SUBCOMMANDS = {"dedup": _dedup}


def memory_handler(args: list[str]) -> None:
    """Entry point for ``arc memory <subcommand>`` (registry dispatch)."""
    dispatch(_build_parser(), _SUBCOMMANDS, args)


__all__ = ["memory_handler"]
