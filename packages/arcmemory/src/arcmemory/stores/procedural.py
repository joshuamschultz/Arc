"""Procedural store — how-to cards promoted by repetition.

A procedure is a glass-box markdown card at ``memory/procedures/<slug>.md``:
frontmatter carries the ``use_count`` (a sequence seen >= threshold gets promoted
to a card and each reuse bumps the count) and the body is the numbered steps.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.mdfile import atomic_write_text, parse_document, render_document
from arcmemory.types import Procedure


class ProceduralStore:
    """Read/write how-to cards for one scope."""

    def __init__(self, workspace: Path) -> None:
        self._dir = Path(workspace) / "memory" / "procedures"

    def path_for(self, slug: str) -> Path:
        """Absolute path to a procedure card."""
        return self._dir / f"{slug}.md"

    def write(self, procedure: Procedure) -> Path:
        """Render a procedure to markdown and atomically write it."""
        frontmatter = {
            "slug": procedure.slug,
            "title": procedure.title,
            "use_count": procedure.use_count,
            "classification": procedure.classification,
        }
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(procedure.steps, start=1))
        body = f"# {procedure.title}\n\n## Steps\n{steps}"
        path = self.path_for(procedure.slug)
        atomic_write_text(path, render_document(frontmatter, body))
        return path

    def read(self, slug: str) -> Procedure | None:
        """Load a procedure card (None if absent)."""
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


__all__ = ["ProceduralStore"]
