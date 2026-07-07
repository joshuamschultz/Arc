"""Fast path — deterministic, zero-LLM capture (SDD 4.3, REQ-010/011/012).

This module has NO import of ``arcllm`` and issues NO embedding: capture is a pure
CPU/IO operation whose cost is constant regardless of store size. A capture:

1. **sanitize -> privacy_filter -> dedup** the untrusted text (security boundary);
2. append a raw ``episodic`` event + a daily-log bullet (order preserved);
3. deterministically **tag entities** (controlled vocabulary + regex);
4. **Hebbian-bump** every co-active entity pair (saturating, salience-carrying);
5. emit a ``memory.captured`` audit event.

The audit ``ts`` on each edge is the event timestamp, so an ``index/rebuild`` replay
over the same stream reproduces the graph byte-identically.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.security import Deduper, content_hash, privacy_filter, sanitize
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.tagging import tag_entities
from arcmemory.types import Event, Scope


class FastCapture:
    """Wires the deterministic capture pipeline for one scope."""

    def __init__(
        self,
        db: MemoryDB,
        workspace: Path,
        scope: Scope,
        graph: WeightedGraph,
        *,
        config: MemoryConfig | None = None,
        audit_sink: AuditSink | None = None,
        seed_vocabulary: Iterable[str] | None = None,
    ) -> None:
        self._scope = scope
        self._workspace = Path(workspace)
        self._episodic = EpisodicStore(db, workspace)
        self._graph = graph
        self._cfg = config or MemoryConfig()
        self._audit = audit_sink if audit_sink is not None else NullSink()
        self._deduper = Deduper(self._cfg.dedup_window)
        self._seed_vocab = set(seed_vocabulary or [])
        self._entities_dir = self._workspace / "memory" / "entities"

    def capture(
        self,
        text: str,
        *,
        kind: str = "observation",
        salience: float = 0.0,
        classification: str = "unclassified",
    ) -> Event | None:
        """Capture one untrusted text; return the ``Event`` or None if deduped.

        Zero LLM, zero embedding. Constant-cost regardless of store size.
        """
        clean = privacy_filter(sanitize(text, max_length=self._cfg.max_event_chars))
        if not clean or self._deduper.is_duplicate(clean):
            return None

        event = Event(
            event_id=uuid.uuid4().hex,
            ts=datetime.now(UTC).isoformat(),
            scope=self._scope.key,
            kind=kind,
            text=clean,
            hash=content_hash(clean),
        )
        event.entities = tag_entities(clean, self._vocabulary())

        self._episodic.append(event)
        self._episodic.append_bullet(event)
        for a, b in combinations(event.entities, 2):
            self._graph.hebbian_bump(self._scope.key, a, b, salience=salience, ts=event.ts)

        self._emit_captured(event, classification)
        return event

    def _vocabulary(self) -> set[str]:
        """Tagging vocabulary: seed terms + the slugs of existing entity files."""
        vocab = set(self._seed_vocab)
        if self._entities_dir.exists():
            vocab.update(p.stem for p in self._entities_dir.glob("*.md"))
        return vocab

    def _emit_captured(self, event: Event, classification: str) -> None:
        """Emit the tamper-evident ``memory.captured`` audit event (AU-2)."""
        emit(
            AuditEvent(
                actor_did=self._scope.agent_did,
                action="memory.captured",
                target=event.event_id,
                outcome="allow",
                classification=classification,
                tier=self._cfg.tier,
                payload_hash=event.hash,
                extra={"kind": event.kind, "entities": event.entities},
            ),
            self._audit,
        )


__all__ = ["FastCapture"]
