"""COMP-001 — source adapter contract (SPEC-073).

Thin per-source-type normalizers: a raw pushed batch of dicts becomes typed
``SourceRecord``s. Adapters are the only source-shaped code in the ingestion
path — sinks and the ``Router`` never inspect vendor-specific payload shapes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from arcmemory.types import SourceRecord


@runtime_checkable
class SourceAdapter(Protocol):
    """Normalizes one source's raw batch into typed ``SourceRecord``s."""

    source_kind: str

    def normalize(self, raw_batch: list[dict[str, object]]) -> list[SourceRecord]: ...


class DictAdapter:
    """Default pass-through adapter: raw dicts already carry SourceRecord fields."""

    source_kind: str = "generic"

    def normalize(self, raw_batch: list[dict[str, object]]) -> list[SourceRecord]:
        return [self._to_record(raw) for raw in raw_batch]

    @staticmethod
    def _to_record(raw: dict[str, object]) -> SourceRecord:
        text = raw.get("text")
        kind = raw.get("kind")
        classification = raw.get("classification")
        source_updated_at = raw.get("source_updated_at")
        salience = raw.get("salience")
        metadata = raw.get("metadata")
        return SourceRecord(
            external_id=str(raw.get("external_id", "")),
            text=str(text) if text is not None else "",
            kind=kind if isinstance(kind, str) else "observation",
            classification=(classification if isinstance(classification, str) else "unclassified"),
            source_updated_at=(source_updated_at if isinstance(source_updated_at, str) else ""),
            salience=float(salience) if isinstance(salience, (int, float)) else 0.0,
            metadata=(
                {str(k): str(v) for k, v in metadata.items()} if isinstance(metadata, dict) else {}
            ),
        )
