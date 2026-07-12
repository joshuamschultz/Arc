"""Insight store — the centerpiece: minted pattern/thesis cards.

An insight is a glass-box markdown card at ``memory/insights/<id>.md`` carrying the
three things a raw episode lacks (SDD 7):

* **trigger** -- the situation stated at the mechanism level (surface stripped),
  the text that gets embedded into abstraction space;
* **cues[]** -- abstract feature tags from the controlled vocabulary (graph nodes);
* **instances[]** -- links to the episodes it generalizes (enrichment targets).

Plus ``confidence``/``salience``/``status`` (``guessed`` on first mint, ``known``
once corroborated). Nothing here calls an LLM -- minting happens in the slow path;
this store only round-trips the card to/from markdown.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.mdfile import atomic_write_text, parse_document, render_document
from arcmemory.slug import canonical_slug
from arcmemory.types import Confidence, Insight


class InsightStore:
    """Read/write insight cards for one scope."""

    def __init__(self, workspace: Path) -> None:
        self._dir = Path(workspace) / "memory" / "insights"

    def path_for(self, insight_id: str) -> Path:
        """Absolute path to an insight card (id canonicalized)."""
        return self._dir / f"{canonical_slug(insight_id)}.md"

    def write(self, insight: Insight) -> Path:
        """Render an insight to markdown and atomically write it."""
        frontmatter = {
            "id": insight.id,
            "trigger": insight.trigger,
            "cues": insight.cues,
            "instances": insight.instances,
            "classification": insight.classification,
            "confidence": insight.confidence,
            "salience": insight.salience,
            "status": insight.status.value,
            "hits": insight.hits,
        }
        body = f"# {insight.id}\n\n## Statement\n{insight.statement}"
        path = self.path_for(insight.id)
        atomic_write_text(path, render_document(frontmatter, body))
        return path

    def read(self, insight_id: str) -> Insight | None:
        """Load an insight card (None if absent)."""
        path = self.path_for(insight_id)
        if not path.exists():
            return None
        fm, body = parse_document(path.read_text(encoding="utf-8"))
        statement = ""
        if "## Statement" in body:
            statement = body.split("## Statement", 1)[1].strip()
        return Insight(
            id=str(fm.get("id", insight_id)),
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

    def all_ids(self) -> list[str]:
        """Every insight id currently on disk (sorted)."""
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.md"))


__all__ = ["InsightStore"]
