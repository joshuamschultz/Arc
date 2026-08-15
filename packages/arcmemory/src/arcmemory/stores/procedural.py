"""Procedural store — how-to cards distilled from the session conversation.

A procedure is a glass-box markdown card at ``memory/procedures/<slug>.md`` holding a
reusable METHOD — the USER's way of doing something (how they research a keyword, how
a stock is analyzed, how a customer is quoted under certain conditions). It is a
mini-skill the agent authors once and then EDITS across sessions, so the card holds the
accumulated method, not this session's mention of it.

That makes the merge rule the heart of this store: :meth:`ProceduralStore.upsert` folds
an incoming step list into the stored one. The incoming list is authoritative for
wording and order, a step it simply does not mention is KEPT in place, and a step is
removed only when it is named in ``dropped``. Frontmatter carries two counters that
answer different questions — ``use_count`` (times the playbook was reached for, bumped
by :meth:`ProceduralStore.increment_use`) and ``revisions`` (times it was written or
evolved) — and the body is the numbered steps, which may carry ``[[slug]]`` links to
the entities/tools they involve.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from arcmemory.mdfile import atomic_write_text, parse_document, render_document
from arcmemory.slug import canonical_slug
from arcmemory.stores.semantic import extract_wiki_links
from arcmemory.types import Procedure, ProcedureSummary, Step


def _norm(step: str | Step) -> str:
    """Whitespace/case-insensitive key for matching a step against a stored one."""
    return " ".join(str(step).split()).casefold()


def _unique(steps: list[Step]) -> list[Step]:
    """Drop repeated steps, keeping the first wording seen (stable)."""
    seen: set[str] = set()
    out: list[Step] = []
    for step in steps:
        key = _norm(step)
        if key not in seen:
            seen.add(key)
            out.append(step)
    return out


def merge_steps(existing: list[Step], incoming: list[str], dropped: Sequence[str]) -> list[Step]:
    """Fold an incoming step list into the stored one — omission never deletes.

    The incoming list is authoritative for what it names: rewording, reordering, and
    inserting all land exactly as written. A stored step the caller did not mention
    re-enters ahead of its ANCHOR — the next stored step the caller did mention — so it
    keeps its place in the method instead of being dumped at the end; unmentioned steps
    that trail the last anchor close out the card. A step disappears only when it
    appears in ``dropped``, the explicit signal that the conversation abandoned it.

    A step the session restates carries its corroboration forward and gains one hit —
    matched on the same normalized key the merge uses, so a rewording keeps the
    evidence it has accumulated instead of resetting to a first sighting. A step left
    unmentioned keeps its standing: silence is not disagreement, and decaying it would
    erode every part of a procedure a session did not happen to touch.
    """
    dropped_keys = {_norm(step) for step in dropped}
    incoming_keys = {_norm(step) for step in incoming}
    stored_hits = {_norm(step): step.hits for step in existing}
    before_anchor: dict[str, list[Step]] = {}
    pending: list[Step] = []
    for step in (s for s in existing if _norm(s) not in dropped_keys):
        key = _norm(step)
        if key not in incoming_keys:
            pending.append(step)
        elif pending:
            before_anchor.setdefault(key, []).extend(pending)
            pending = []

    merged: list[Step] = []
    for text in incoming:
        key = _norm(text)
        merged.extend(before_anchor.pop(key, []))
        merged.append(Step(text=text, hits=stored_hits.get(key, 0) + 1))
    merged.extend(pending)  # stored steps after the last anchor
    return _unique(merged)


#: Trailing corroboration marker on a rendered step: ``run the tests  x3``.
_HITS_RE = re.compile(r"\s+x(\d+)$")


def format_hits(hits: int) -> str:
    """Render a step's corroboration compactly and legibly (``x3``)."""
    return f"x{max(1, hits)}"


def parse_step(rendered: str) -> Step:
    """Read one rendered step back, with or without its counter.

    Cards written before steps carried corroboration are plain numbered lines, and
    one hit is the honest reading of a step recorded at least once — a parser that
    demanded the marker would drop every step of every card already on disk.
    """
    match = _HITS_RE.search(rendered)
    if match is None:
        return Step(text=rendered, hits=1)
    return Step(text=rendered[: match.start()].strip(), hits=int(match.group(1)))


def procedure_link_targets(procedure: Procedure) -> list[str]:
    """Every ``[[slug]]`` the card points at (sorted) — its edges in the shared graph.

    A procedure lives in the same one-namespace graph as entities, insights and cues, so
    wiring its slug to the entities/tools its steps name is what makes the method
    reachable by spreading activation when a like situation recurs.
    """
    text = "\n".join([procedure.title, procedure.when_to_use, *(s.text for s in procedure.steps)])
    return sorted(set(extract_wiki_links(text)))


class ProceduralStore:
    """Read/write how-to cards for one scope."""

    def __init__(self, workspace: Path) -> None:
        self._dir = Path(workspace) / "memory" / "procedures"

    def path_for(self, slug: str) -> Path:
        """Absolute path to a procedure card (slug canonicalized)."""
        return self._dir / f"{canonical_slug(slug)}.md"

    def write(self, procedure: Procedure) -> Path:
        """Render a procedure to markdown and atomically write it."""
        frontmatter = {
            "slug": procedure.slug,
            "title": procedure.title,
            "when_to_use": procedure.when_to_use,
            "use_count": procedure.use_count,
            "revisions": procedure.revisions,
            "classification": procedure.classification,
            # Dates the card so an index rebuild replays its link edges with the same
            # timestamp the live write used (mirrors the entity card).
            "last_updated": datetime.now(UTC).strftime("%Y-%m-%d"),
        }
        steps = "\n".join(
            f"{i}. {step.text} {format_hits(step.hits)}"
            for i, step in enumerate(procedure.steps, start=1)
        )
        when = f"## When to use\n{procedure.when_to_use}\n\n" if procedure.when_to_use else ""
        body = f"# {procedure.title}\n\n{when}## Steps\n{steps}"
        path = self.path_for(procedure.slug)
        atomic_write_text(path, render_document(frontmatter, body))
        return path

    def upsert(
        self,
        slug: str,
        title: str,
        *,
        when_to_use: str = "",
        steps: list[str],
        dropped: Sequence[str] = (),
        classification: str = "unclassified",
    ) -> Procedure:
        """Create or EVOLVE a card: steps merge (:func:`merge_steps`), ``revisions`` bumps.

        A blank ``title``/``when_to_use`` keeps the stored one, so a session that refines
        only the steps cannot blank out the trigger the card is found by.
        """
        slug = canonical_slug(slug)
        existing = self.read(slug)
        procedure = Procedure(
            slug=slug,
            title=title or (existing.title if existing else slug.replace("-", " ").title()),
            when_to_use=when_to_use or (existing.when_to_use if existing else ""),
            steps=merge_steps(existing.steps if existing else [], steps, dropped),
            # Writing a procedure is NOT using it. Bumping use_count here is what made
            # a much-refined card indistinguishable from a much-used one, and left a
            # live store with no usage signal at all.
            use_count=existing.use_count if existing else 0,
            revisions=(existing.revisions + 1) if existing else 1,
            classification=classification,
        )
        self.write(procedure)
        return procedure

    def read(self, slug: str) -> Procedure | None:
        """Load a procedure card (None if absent)."""
        slug = canonical_slug(slug)
        path = self.path_for(slug)
        if not path.exists():
            return None
        fm, body = parse_document(path.read_text(encoding="utf-8"))
        steps = [
            parse_step(line.split(". ", 1)[1].strip())
            for line in body.splitlines()
            if line.strip() and line.strip()[0].isdigit() and ". " in line
        ]
        return Procedure(
            slug=str(fm.get("slug", slug)),
            title=str(fm.get("title", slug.replace("-", " ").title())),
            when_to_use=str(fm.get("when_to_use", "")),
            steps=steps,
            use_count=int(fm.get("use_count", 0)),
            # Absent on every card written before the two counters were split, and
            # 0 is the truthful answer for those: nothing recorded their revisions.
            revisions=int(fm.get("revisions", 0)),
            classification=str(fm.get("classification", "unclassified")),
        )

    def list_summaries(self) -> list[ProcedureSummary]:
        """Every procedure as slug + trigger + counters, WITHOUT its steps.

        The index an agent scans to decide whether a playbook already covers the task
        in front of it. Steps are deliberately absent: a fleet accumulates dozens of
        procedures whose full step lists will not fit in a turn, and the trigger is
        what decides relevance. Read the one card that matches.
        """
        cards = (self.read(slug) for slug in self.slugs())
        return [
            ProcedureSummary(
                slug=card.slug,
                title=card.title,
                when_to_use=card.when_to_use,
                use_count=card.use_count,
                revisions=card.revisions,
            )
            for card in cards
            if card is not None
        ]

    def increment_use(self, slug: str) -> int:
        """Bump a card's use-count; return the new count (0 if the card is absent).

        Called when a procedure is actually reached for. This existed and nothing in
        production ever called it, so ``use_count`` only ever moved on writes — which
        is why a live store of 36 procedures held no evidence that any had been used.
        """
        procedure = self.read(slug)
        if procedure is None:
            return 0
        procedure.use_count += 1
        self.write(procedure)
        return procedure.use_count

    def slugs(self) -> list[str]:
        """Every procedure slug currently on disk (sorted)."""
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.md"))


__all__ = ["ProceduralStore", "merge_steps", "procedure_link_targets"]
