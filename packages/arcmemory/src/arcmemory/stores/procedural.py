"""Procedural store — how-to cards promoted by repetition.

A procedure is a glass-box markdown card at ``memory/procedures/<slug>.md``:
frontmatter carries the ``use_count`` (a sequence seen >= threshold gets promoted
to a card and each reuse bumps the count) and the body is the numbered steps.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

from arcmemory.mdfile import atomic_write_text, parse_document, render_document
from arcmemory.types import Event, Procedure

_ACTION_KIND = "action"


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

    def promote(
        self, events: list[Event], *, threshold: int = 2, min_len: int = 2
    ) -> list[Procedure]:
        """Promote action-sequences seen >= ``threshold`` times to how-to cards.

        The action stream (events of kind ``action``) is split into runs at every
        non-action boundary; an identical run of length >= ``min_len`` that recurs
        at least ``threshold`` times becomes (or reinforces) a procedure whose
        ``use_count`` is the number of occurrences. Zero-LLM and deterministic.
        """
        counts = Counter(self._action_runs(events, min_len=min_len))
        promoted: list[Procedure] = []
        for steps, occurrences in counts.items():
            if occurrences < threshold:
                continue
            procedure = Procedure(
                slug=_steps_slug(steps),
                title=" then ".join(steps),
                steps=list(steps),
                use_count=occurrences,
            )
            self.write(procedure)
            promoted.append(procedure)
        return promoted

    @staticmethod
    def _action_runs(events: list[Event], *, min_len: int) -> list[tuple[str, ...]]:
        """Contiguous runs of action-event texts, split at non-action boundaries."""
        runs: list[tuple[str, ...]] = []
        current: list[str] = []
        for event in events:
            if event.kind == _ACTION_KIND:
                current.append(event.text)
                continue
            if len(current) >= min_len:
                runs.append(tuple(current))
            current = []
        if len(current) >= min_len:
            runs.append(tuple(current))
        return runs

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


def _steps_slug(steps: tuple[str, ...]) -> str:
    """Stable slug for a step-sequence (deterministic across runs)."""
    digest = hashlib.sha256("\n".join(steps).encode("utf-8")).hexdigest()[:12]
    return f"proc-{digest}"


__all__ = ["ProceduralStore"]
