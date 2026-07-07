"""T-070..T-072 — retrieve orchestration + classification-gated recall.

These exercise the real end-to-end path: index curated markdown into the surface
channel, then ``Retriever.retrieve`` fuses surface + structural, confidence-gates,
applies the no-read-up gate (reusing ``arctrust.dominates``), and returns a
boundary-marked, budget-bounded ``Bundle``. Classification labels enter through
entity frontmatter (``SessionACL.classification``-style), exactly as production.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from arctrust.audit import AuditEvent
from arctrust.classification import Classification

from arcmemory.db import MemoryDB
from arcmemory.retrieve import Retriever
from arcmemory.types import Scope, Situation

_DIMS = 8


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _StubEmbedder:
    """Deterministic embedder so the surface vec channel is available in tests."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            out.append([digest[i] / 255.0 for i in range(_DIMS)])
        return out


def _write_entity(workspace: Path, slug: str, classification: str, body: str) -> None:
    """Write a curated entity markdown file with a classification label."""
    path = workspace / "memory" / "entities" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nslug: {slug}\nclassification: {classification}\n---\n\n{body}\n",
        encoding="utf-8",
    )


async def _retriever(
    workspace: Path, db: MemoryDB, scope: Scope, sink: _RecordingSink
) -> Retriever:
    rv = Retriever(db, workspace, scope, embedder=_StubEmbedder(), audit_sink=sink)
    await rv.index()  # embed + FTS the curated files
    return rv


# -- T-070: no-read-up gate, end-to-end -------------------------------------


async def test_secret_memory_dropped_for_unclassified_caller(workspace, db, scope) -> None:
    _write_entity(workspace, "public-note", "unclassified", "the widget shipping cadence")
    _write_entity(workspace, "black-project", "SECRET", "the widget launch codes")
    sink = _RecordingSink()
    rv = await _retriever(workspace, db, scope, sink)

    bundle = await rv.retrieve(
        Situation(text="widget"), clearance=Classification.UNCLASSIFIED, top_k=5, budget=10_000
    )

    sources = {r.source for r in bundle.recalls}
    assert "file:memory/entities/public-note.md" in sources
    assert "file:memory/entities/black-project.md" not in sources  # SECRET dropped
    # The drop is audited; the bundle leaks no trace of it.
    assert any(e.action == "recall.dropped" for e in sink.events)
    assert "launch codes" not in bundle.text
    assert "black-project" not in bundle.text


async def test_cui_memory_kept_for_secret_caller(workspace, db, scope) -> None:
    _write_entity(workspace, "cui-note", "CUI", "the controlled widget roster")
    sink = _RecordingSink()
    rv = await _retriever(workspace, db, scope, sink)

    bundle = await rv.retrieve(
        Situation(text="widget"), clearance=Classification.SECRET, top_k=5, budget=10_000
    )

    assert "file:memory/entities/cui-note.md" in {r.source for r in bundle.recalls}
    assert not any(e.action == "recall.dropped" for e in sink.events)  # SECRET dominates CUI


# -- T-071: fail-closed + leaks-nothing, end-to-end -------------------------


async def test_unlabeled_rejected_at_federal(workspace, db, scope) -> None:
    from arcmemory.config import MemoryConfig

    # An entity file with NO classification frontmatter -> unlabeled.
    path = workspace / "memory" / "entities" / "mystery.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("the widget of unknown provenance\n", encoding="utf-8")
    sink = _RecordingSink()
    rv = Retriever(
        db,
        workspace,
        scope,
        config=MemoryConfig.for_tier("federal"),
        embedder=_StubEmbedder(),
        audit_sink=sink,
    )
    await rv.index()

    bundle = await rv.retrieve(
        Situation(text="widget"), clearance=Classification.TOP_SECRET, top_k=5, budget=10_000
    )

    assert bundle.recalls == []  # fail-closed despite top clearance
    assert any(e.extra.get("reason") == "unlabeled_fail_closed" for e in sink.events)


async def test_unlabeled_defaulted_at_personal(workspace, db, scope) -> None:
    path = workspace / "memory" / "entities" / "mystery.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("the widget of unknown provenance\n", encoding="utf-8")
    sink = _RecordingSink()
    rv = await _retriever(workspace, db, scope, sink)  # personal by default

    bundle = await rv.retrieve(
        Situation(text="widget"), clearance=Classification.UNCLASSIFIED, top_k=5, budget=10_000
    )

    assert "file:memory/entities/mystery.md" in {r.source for r in bundle.recalls}


# -- T-072: boundary-mark + budget, end-to-end ------------------------------


async def test_bundle_items_are_boundary_wrapped(workspace, db, scope) -> None:
    _write_entity(workspace, "note-a", "unclassified", "the widget cadence report")
    _write_entity(workspace, "note-b", "unclassified", "the widget roster memo")
    sink = _RecordingSink()
    rv = await _retriever(workspace, db, scope, sink)

    bundle = await rv.retrieve(
        Situation(text="widget"), clearance=Classification.UNCLASSIFIED, top_k=5, budget=10_000
    )

    assert len(bundle.recalls) >= 2
    assert bundle.text.count("<memory-result") == len(bundle.recalls)


async def test_over_budget_bundle_truncates_from_the_bottom(workspace, db, scope) -> None:
    for i in range(5):
        _write_entity(workspace, f"note-{i}", "unclassified", f"the widget fact number {i}")
    sink = _RecordingSink()
    rv = await _retriever(workspace, db, scope, sink)

    bundle = await rv.retrieve(
        Situation(text="widget"), clearance=Classification.UNCLASSIFIED, top_k=5, budget=80
    )

    assert bundle.truncated is True
    assert len(bundle.recalls) < 5  # bottom-ranked items dropped to fit budget
