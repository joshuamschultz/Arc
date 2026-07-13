"""Procedural store — how-to cards distilled from the session conversation.

A procedure is a glass-box markdown card at ``memory/procedures/<slug>.md`` holding a
reusable METHOD — an explicit or implicit way of doing something the session revealed
(how a stock is analyzed, how risk is managed, how a customer is quoted under certain
conditions). Frontmatter carries the ``use_count`` (bumped each time the method is
re-seen) and the body is the numbered steps. Methods EVOLVE: re-``upsert``-ing an
existing procedure updates its steps (added / removed / modified) in place.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.mdfile import atomic_write_text, parse_document, render_document
from arcmemory.slug import canonical_slug
from arcmemory.types import Procedure


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
            "classification": procedure.classification,
        }
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(procedure.steps, start=1))
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
        classification: str = "unclassified",
    ) -> Procedure:
        """Create or refresh an LLM-extracted procedure; bump use_count on re-extract."""
        slug = canonical_slug(slug)
        existing = self.read(slug)
        procedure = Procedure(
            slug=slug,
            title=title,
            when_to_use=when_to_use,
            steps=steps,
            use_count=(existing.use_count + 1) if existing else 1,
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
            line.split(". ", 1)[1].strip()
            for line in body.splitlines()
            if line.strip() and line.strip()[0].isdigit() and ". " in line
        ]
        return Procedure(
            slug=str(fm.get("slug", slug)),
            title=str(fm.get("title", slug.replace("-", " ").title())),
            when_to_use=str(fm.get("when_to_use", "")),
            steps=steps,
            use_count=int(fm.get("use_count", 0)),
            classification=str(fm.get("classification", "unclassified")),
        )

    def increment_use(self, slug: str) -> int:
        """Bump a card's use-count; return the new count (0 if the card is absent)."""
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


__all__ = ["ProceduralStore"]
