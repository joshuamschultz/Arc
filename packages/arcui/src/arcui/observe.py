"""Observe plane — arcui reads operational history from the arcstore database.

SPEC-026 FR-5 (full push teardown): arcui is a pure reader of the durable
record. It runs its own ``StoreIngest`` over the shared spool + WORM files
(everything arcllm/arcrun/arcagent wrote, whether or not arcui was running) into
its own SQLite mirror (shared-nothing, NFR-8), then serves read-on-demand REST
from that mirror. No live push wire, nothing to drop.

Stats are computed directly from the store on read (one pass over the window) —
there is no separate rolling aggregator to keep in sync.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arcstore.backends import open_backend
from arcstore.config import resolve_data_dir
from arcstore.ingest import StoreIngest

from arcui.observe_stats import (
    compute_cost_efficiency,
    compute_performance,
    compute_stats,
    compute_timeseries,
)

_WINDOW_SECONDS = {
    "1h": 3600,
    "24h": 86_400,
    "7d": 604_800,
    "30d": 2_592_000,
}


def _window_cutoff(window: str) -> str:
    """ISO-8601 UTC cutoff for a window key (lexicographic compare is valid)."""
    seconds = _WINDOW_SECONDS.get(window, _WINDOW_SECONDS["24h"])
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _row_to_trace(row: dict[str, Any]) -> dict[str, Any]:
    """Map an arcstore ``llm_calls`` row to the UI trace shape."""
    prompt = row.get("prompt_tokens") or 0
    completion = row.get("completion_tokens") or 0
    outcome = row.get("outcome")
    return {
        "trace_id": row.get("record_id"),
        "timestamp": row.get("ts"),
        "model": row.get("model"),
        "provider": row.get("provider"),
        "agent": row.get("actor_did"),
        "agent_label": row.get("agent_label"),
        # UI vocabulary: the producer records ``ok``/``error`` outcomes.
        "status": "success" if outcome == "ok" else outcome,
        "cost_usd": row.get("cost_usd"),
        "duration_ms": row.get("latency_ms"),
        "input_tokens": row.get("prompt_tokens"),
        "output_tokens": row.get("completion_tokens"),
        "total_tokens": prompt + completion,
        "request_id": row.get("request_id"),
    }


class Observe:
    """arcui's read-only view of the durable operational record.

    Owns a per-instance SQLite mirror and the ingest task that keeps it current
    by tailing the shared spool + WORM files. Lifecycle is managed by the server
    lifespan (``start``/``stop``); all reads are synchronous request/response.
    """

    def __init__(self, *, data_dir: Path | None = None, backend: str = "sqlite") -> None:
        base = data_dir if data_dir is not None else resolve_data_dir()
        self._data_dir = base
        # Backend selected by name via the factory — Observe only ever depends on
        # the StorageBackend Protocol, so switching storage (Phase 5 config) does
        # not touch this read plane.
        self._backend = open_backend(backend, base / "store" / "arcui.db")
        self._ingest = StoreIngest(
            self._backend,
            spool_dir=base / "spool",
            worm_dir=base / "worm",
        )
        self._started = False

    async def _ensure(self) -> None:
        """Ensure the mirror schema exists before a read (idempotent).

        Reads must succeed even when the server lifespan never ran (e.g. a
        ``TestClient`` used without a context manager) — they just return empty.
        """
        if not self._started:
            await self._backend.start()
            self._started = True

    async def start(self) -> None:
        """Open the mirror, backfill from durable files, start tailing."""
        await self._ensure()
        await self._ingest.start()

    async def stop(self) -> None:
        """Stop tailing and release the mirror."""
        await self._ingest.stop()
        await self._backend.stop()

    async def refresh(self) -> None:
        """Force a one-shot ingest scan (used by tests / on-demand reads)."""
        await self._ingest.scan_once()

    # -- reads -------------------------------------------------------------

    async def traces(
        self,
        *,
        agent: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        await self._ensure()
        # The UI's "agent" identifier is the human agent label (arcagent name),
        # not the full DID; filter on that.
        where = {"agent_label": agent} if agent else None
        rows = await self._backend.query(
            "llm_calls", where=where, order_by="ts DESC", limit=limit
        )
        return [_row_to_trace(r) for r in rows]

    async def trace(self, trace_id: str) -> dict[str, Any] | None:
        await self._ensure()
        rows = await self._backend.query("llm_calls", where={"record_id": trace_id}, limit=1)
        return _row_to_trace(rows[0]) if rows else None

    async def run_events(
        self, *, agent: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        await self._ensure()
        where = {"actor_did": agent} if agent else None
        return await self._backend.query(
            "run_events", where=where, order_by="ts DESC", limit=limit
        )

    async def audit(self, *, agent: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        await self._ensure()
        where = {"actor_did": agent} if agent else None
        return await self._backend.query(
            "audit_chain", where=where, order_by="seq DESC", limit=limit
        )

    async def _llm_rows_in_window(
        self, window: str, *, agent: str | None = None
    ) -> list[dict[str, Any]]:
        """All ``llm_calls`` rows within ``window`` (optionally one agent)."""
        await self._ensure()
        where = {"agent_label": agent} if agent else None
        cutoff = _window_cutoff(window)
        rows = await self._backend.query(
            "llm_calls", where=where, order_by="ts DESC", limit=100_000
        )
        return [r for r in rows if (r.get("ts") or "") >= cutoff]

    async def stats(self, window: str = "24h", *, agent: str | None = None) -> dict[str, Any]:
        """Aggregate LLM telemetry over a window directly from the store.

        Replaces the RollingAggregator: the database *is* the aggregate, so we
        compute the rollup on read in a single pass (read-on-demand is cheap).
        """
        rows = await self._llm_rows_in_window(window, agent=agent)
        return compute_stats(rows, window=window)

    async def timeseries(
        self, window: str = "24h", *, agent: str | None = None
    ) -> dict[str, Any]:
        rows = await self._llm_rows_in_window(window, agent=agent)
        return compute_timeseries(rows, window=window)

    async def performance(
        self, window: str = "24h", *, agent: str | None = None
    ) -> dict[str, Any]:
        rows = await self._llm_rows_in_window(window, agent=agent)
        return compute_performance(rows, window=window)

    async def cost_efficiency(
        self, window: str = "24h", *, agent: str | None = None
    ) -> dict[str, Any]:
        rows = await self._llm_rows_in_window(window, agent=agent)
        return compute_cost_efficiency(rows, window=window)
