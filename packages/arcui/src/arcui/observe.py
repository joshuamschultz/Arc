"""Observe plane — arcui reads operational history from the arcstore database.

SPEC-026 FR-5 (full push teardown): arcui is a pure reader of the durable
record. It runs its own ``StoreIngest`` over the shared spool + WORM files
(everything arcllm/arcrun/arcagent wrote, whether or not arcui was running) into
its own PostgreSQL-backed operational view (shared-nothing, NFR-8), then serves read-on-demand REST
from that mirror. No live push wire, nothing to drop.

Stats are computed directly from the store on read (one pass over the window) —
there is no separate rolling aggregator to keep in sync.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from arcstore import query as store_query
from arcstore.backends import ArcStoreBackend, open_backend
from arcstore.config import ArcStoreConfig, resolve_data_dir
from arcstore.ingest import StoreIngest
from arcstore.tasks import MutableTaskBackend, TaskStore
from pydantic import SecretStr

from arcui.observe_stats import (
    capability_class,
    compute_cost_efficiency,
    compute_llm_by_identity,
    compute_performance,
    compute_runs,
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


def _task_touched_at(task: dict[str, Any]) -> str:
    """A task's most recent activity stamp, for window filtering (H-004).

    ``updated_at`` moves on every status change; ``created_at`` covers a task
    that was created but never touched again. A task with neither (should not
    happen — both are stamped on create) sorts before every real cutoff, so it
    drops out of a windowed read rather than corrupting the comparison.
    """
    return task.get("updated_at") or task.get("created_at") or ""


def _call_job(agent_label: str | None, extra: dict[str, Any]) -> str | None:
    """Name one LLM call's kind for the Calls table (H-029).

    Mirrors ``compute_runs``' ``_job_of``, at call granularity instead of
    run granularity: a maintenance call labels itself ``<agent>/<job>``
    (workpad, distill, consolidate, eval, …) — the suffix is the job. Absent
    a suffix, a background self-wake (pulse/scheduler/sub-agent) reads as
    ``"background"``; a real, person-driven call gets no badge.
    """
    if isinstance(agent_label, str) and "/" in agent_label:
        suffix = agent_label.rsplit("/", 1)[1].strip()
        if suffix:
            return suffix
    if extra.get("origin") == "background":
        return "background"
    return None


def _row_to_trace(row: dict[str, Any], *, include_bodies: bool = False) -> dict[str, Any]:
    """Map an arcstore ``llm_calls`` row to the UI trace shape.

    The LIST is metadata-only (``include_bodies=False``): the raw request/response
    payloads (parked in ``extra`` when ``store_raw_bodies`` is on) can be ~100KB+
    EACH, so shipping them for a whole page is a multi-MB response the drawer would
    re-fetch per-row anyway (it calls the single-trace endpoint on open). The
    single-trace DETAIL sets ``include_bodies=True`` to surface ``request`` /
    ``response`` / ``messages`` / ``tools``. Under the federal/CUI default the bodies
    are absent regardless — the UI handles that.

    ``capability_class`` / ``operation`` / ``job`` (H-029) are cheap, string-only
    labels — unlike the raw bodies they ship on every row, list included, so the
    Calls table can say what a call WAS without a per-row detail fetch.
    """
    prompt = row.get("prompt_tokens") or 0
    completion = row.get("completion_tokens") or 0
    outcome = row.get("outcome")
    extra = row.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (json.JSONDecodeError, TypeError):
            extra = None
    extra = extra if isinstance(extra, dict) else {}
    agent_label = row.get("agent_label")
    trace: dict[str, Any] = {
        "trace_id": row.get("record_id"),
        "timestamp": row.get("ts"),
        "model": row.get("model"),
        "provider": row.get("provider"),
        "agent": row.get("actor_did"),
        "agent_label": agent_label,
        # UI vocabulary: the producer records ``ok``/``error`` outcomes.
        "status": "success" if outcome == "ok" else outcome,
        "cost_usd": row.get("cost_usd"),
        "duration_ms": row.get("latency_ms"),
        "input_tokens": row.get("prompt_tokens"),
        "output_tokens": row.get("completion_tokens"),
        "total_tokens": prompt + completion,
        # Cache breakdown (SPEC-029) — lets a consumer compute hit-rate =
        # cache_read / (input + cache_read). None when the provider reported none.
        "cache_read_tokens": row.get("cache_read_tokens"),
        "cache_write_tokens": row.get("cache_write_tokens"),
        "request_id": row.get("request_id"),
        # H-028's inference/embedding split, reused (not recomputed) per call.
        "capability_class": capability_class({"extra": extra}),
        # The embed path's short caller label (``embed:*`` / ``retrieve:*``);
        # None for a chat/completion call, which never stamps one.
        "operation": extra.get("operation"),
        "job": _call_job(agent_label, extra),
    }
    if not include_bodies:
        return trace
    request_body = extra.get("request_body")
    trace["request"] = request_body
    trace["response"] = extra.get("response_body")
    trace["messages"] = (request_body or {}).get("messages")
    trace["tools"] = (request_body or {}).get("tools")
    return trace


def _spawn_node(
    did: str, edge: dict[str, Any] | None, children: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "did": did,
        "role": edge.get("role") if edge else None,
        "depth": edge.get("depth") if edge else 0,
        "outcome": edge.get("outcome") if edge else None,
        "children": children,
    }


def _build_spawn_tree(edges: list[dict[str, Any]], root_did: str | None) -> dict[str, Any]:
    """Rebuild a parent→child tree from flat spawn_event rows (depth-bounded)."""
    by_parent: dict[str, list[dict[str, Any]]] = {}
    edge_for: dict[str, dict[str, Any]] = {}
    children_dids: set[str] = set()
    for e in edges:
        parent, child = e.get("parent_did"), e.get("child_did")
        if not parent or not child:
            continue
        by_parent.setdefault(parent, []).append(e)
        edge_for[child] = e
        children_dids.add(child)

    if root_did is None:
        roots = [p for p in by_parent if p not in children_dids]
        root_did = roots[0] if roots else ""

    # max_depth=3 caps recursion; ``seen`` guards against any cyclic edge.
    def _node(did: str, seen: frozenset[str]) -> dict[str, Any]:
        kids = [] if did in seen else by_parent.get(did, [])
        children = [_node(e["child_did"], seen | {did}) for e in kids]
        return _spawn_node(did, edge_for.get(did), children)

    return _node(root_did, frozenset())


class Observe:
    """arcui's read-only view of the durable operational record.

    Owns a per-instance ArcStore mirror and the ingest task that keeps it current
    by tailing the shared spool + WORM files. Lifecycle is managed by the server
    lifespan (``start``/``stop``); all reads are synchronous request/response.
    """

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        backend: ArcStoreBackend | None = None,
        arcstore_config: ArcStoreConfig | None = None,
        arcstore_secret: SecretStr | None = None,
        workspace_dir: Path | None = None,
    ) -> None:
        base = data_dir if data_dir is not None else resolve_data_dir()
        self._data_dir = base
        # Backend selected by name via the factory — Observe only ever depends on
        # the StorageBackend Protocol, so switching storage (Phase 5 config) does
        # not touch this read plane.
        self._owns_backend = backend is None
        self._backend = backend or open_backend(config=arcstore_config, secret=arcstore_secret)
        # workspace_dir enables the arcskill candidate-store + skills-WORM scan
        # (SPEC-054 REQ-120); None keeps the ingest on spool + audit WORM only.
        self._ingest = StoreIngest(
            self._backend,
            spool_dir=base / "spool",
            worm_dir=base / "worm",
            workspace_dir=workspace_dir,
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
        if self._owns_backend:
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
        # H-008: filter on the agent's DID, not its free-text label. A label
        # (config ``[agent].name`` / roster slug) can drift from whatever
        # string got recorded on a call historically (rename, re-casing, a
        # "twin" agent reusing a display name); the DID never does. Callers
        # resolve the caller-facing id to a DID via ``arcui.identity`` before
        # reaching here (see ``routes.agent_detail._common._agent_did``).
        where = {"actor_did": agent} if agent else None
        rows = await self._backend.query("llm_calls", where=where, order_by="ts DESC", limit=limit)
        return [_row_to_trace(r) for r in rows]  # list = metadata only (lightweight)

    async def trace(self, trace_id: str) -> dict[str, Any] | None:
        await self._ensure()
        rows = await self._backend.query("llm_calls", where={"record_id": trace_id}, limit=1)
        # Single-trace detail includes the full request/response bodies.
        return _row_to_trace(rows[0], include_bodies=True) if rows else None

    async def audit(
        self, *, agent: str | None = None, target: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        await self._ensure()
        where: dict[str, Any] = {}
        if agent:
            where["actor_did"] = agent
        if target:
            where["target"] = target
        # Order by ``ts``, not the WORM chain's own ``seq``: this table mirrors
        # MANY chains (one per agent, plus ``audit-chain-arcui.jsonl``), each with
        # its own sequence numbering starting at 0, so "seq DESC" would interleave
        # unrelated chains by local position instead of real time. ``ts`` is also
        # the only ordering the production PostgresBackend actually supports
        # (H-021: "seq DESC" raised ValueError there — the in-memory test fake
        # accepts any column name, which is why this only broke against Postgres).
        return await self._backend.query(
            "audit_chain", where=where or None, order_by="ts DESC", limit=limit
        )

    async def run_recalls(self, run_id: str) -> list[dict[str, Any]]:
        """Recall-attribution events correlated to one run (SPEC-073 Phase D2).

        Filters the durable ``audit_chain`` mirror to ``memory.recall_attributed``
        events stamped with this run's ``request_id`` — the cards/trigger the
        memory brain surfaced during this run, for the run drawer.

        Ordered by ``ts`` (see :meth:`audit` for why not ``seq``).
        """
        await self._ensure()
        return await self._backend.query(
            "audit_chain",
            where={"request_id": run_id, "action": "memory.recall_attributed"},
            order_by="ts DESC",
            limit=50,
        )

    async def tasks(
        self,
        *,
        owner_did: str | None = None,
        status: str | None = None,
        window: str | None = None,
    ) -> list[dict[str, Any]]:
        """Task rows from the arcstore mutable plane (SPEC-056 Phase D, FR-6).

        Reads through the same ``TaskStore`` seam arcagent writes with. The
        mutable-plane methods aren't on the shared ``StorageBackend`` Protocol
        yet (SPEC-032 migration — see ``arcstore.tasks`` docstring), so the
        backend is cast to the narrow ``MutableTaskBackend`` Protocol
        ``TaskStore`` actually needs; the configured backend implements both.

        ``window`` (H-004), when given, keeps only tasks touched (created or
        updated) within it — Home's "today" card wants the backlog moved
        today, not the whole board's all-time total. Filtered in Python: the
        mutable-records plane has no ``ts_gte`` pushdown (unlike the
        append-only operational tables — see :meth:`_llm_rows_in_window`), and
        a fleet's task count is small enough (low hundreds, not events-per-
        second) that this never becomes the O(all-history) cost H-006 is
        about. The Tasks board (``/tasks``) and per-agent tabs call this with
        no window and keep seeing every task, same as before.
        """
        await self._ensure()
        store = TaskStore(cast(MutableTaskBackend, self._backend))
        rows = await store.list(status=status, owner_did=owner_did)
        items = [t.model_dump(mode="json") for t in rows]
        if window is not None:
            cutoff = _window_cutoff(window)
            items = [t for t in items if _task_touched_at(t) >= cutoff]
        return items

    async def _llm_rows_in_window(
        self, window: str, *, agent: str | None = None
    ) -> list[dict[str, Any]]:
        """All ``llm_calls`` rows within ``window`` (optionally one agent).

        The ``ts >= cutoff`` bound is pushed into the store so the window filter
        runs in SQL and the ``limit`` applies after it — not over the whole table.

        H-008: ``agent``, when given, must already be a DID — not a free-text
        label — for the same reason :meth:`traces` filters on ``actor_did``.
        """
        await self._ensure()
        where = {"actor_did": agent} if agent else None
        return await self._backend.query(
            "llm_calls",
            where=where,
            ts_gte=_window_cutoff(window),
            order_by="ts DESC",
            limit=100_000,
        )

    async def stats(self, window: str = "24h", *, agent: str | None = None) -> dict[str, Any]:
        """Aggregate LLM telemetry over a window directly from the store.

        Replaces the RollingAggregator: the database *is* the aggregate, so we
        compute the rollup on read in a single pass (read-on-demand is cheap).
        """
        rows = await self._llm_rows_in_window(window, agent=agent)
        return compute_stats(rows, window=window)

    async def timeseries(self, window: str = "24h", *, agent: str | None = None) -> dict[str, Any]:
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

    # -- SPEC-028 tool / code / spawn surfaces (FR-4) ----------------------

    async def runs(
        self,
        *,
        agent: str | None = None,
        window: str | None = None,
        limit: int = 200,
        scan: int = 4_000,
    ) -> list[dict[str, Any]]:
        """List real runs (one per ``request_id``), newest first.

        Folds run/tool/llm rows into per-run summaries on read — the durable
        record *is* the run list, so there is no session-file scanning. ``scan``
        bounds how many recent rows per table are folded; ``limit`` caps runs.

        ``scan`` is deliberately modest: this endpoint polls every few seconds and
        every table it reads shares one connection pool with approvals and stats,
        so a 20k-row-per-table fold (60k rows a poll) both dragged the page to
        multi-second loads and starved the pool until sibling panels failed. Four
        thousand recent rows per table reconstructs far more than the ``limit`` of
        200 runs the page shows, at a fraction of the load.

        ``window`` (H-004/H-006), when given, pushes a ``ts >= cutoff`` bound
        into each table query (SQL-side, same as :meth:`_llm_rows_in_window`)
        instead of relying on ``scan``'s raw-row-count cap alone — Home's
        "today" card wants runs from the last day, not whatever the last N
        events happen to span. Omitted (the ArcRun page, agent-detail runs
        tab), this reads the full recent history exactly as before.

        The three per-table reads are independent — fired concurrently
        (H-006) rather than three sequential round-trips through the shared
        pool.
        """
        await self._ensure()
        where = {"actor_did": agent} if agent else None
        ts_gte = _window_cutoff(window) if window else None
        kinds = ("run_events", "tool_events", "llm_calls")
        results = await asyncio.gather(
            *(
                self._backend.query(
                    kind, where=where, ts_gte=ts_gte, order_by="ts DESC", limit=scan
                )
                for kind in kinds
            )
        )
        rows = [row for table_rows in results for row in table_rows]
        return compute_runs(rows, limit=limit)

    async def timeline(self, *, run_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        """Merged per-run timeline: llm_call + run_event + tool_event by ``ts``.

        The three streams join on ``request_id == run_id`` (§11.4); merge happens
        in Python (one query per table) — no SQL UNION, matching the Observe shape.
        spawn_events join the same way (request_id is stamped from the spawning
        run's context), so a run's sub-agent spawns show inline in its own
        trace — this run's children, not the agent's lifetime history. The four
        per-table reads are independent — fired concurrently (H-006).
        """
        await self._ensure()
        kinds = ("run_events", "tool_events", "llm_calls", "spawn_events")
        results = await asyncio.gather(
            *(
                self._backend.query(kind, where={"request_id": run_id}, order_by="ts", limit=limit)
                for kind in kinds
            )
        )
        merged = [row for table_rows in results for row in table_rows]
        merged.sort(key=lambda r: (r.get("ts") or "", r.get("kind") or ""))
        return merged

    async def spawn_tree(
        self, *, root_did: str | None = None, limit: int = 100_000
    ) -> dict[str, Any]:
        """Assemble the parent→child lineage tree from ``spawn_events`` (FR-3).

        Flat edges rebuilt into a tree on read (the universal pattern, §11.2);
        bounded by ``max_depth`` so trees are tiny. When ``root_did`` is omitted,
        the root is any parent that never appears as a child.
        """
        await self._ensure()
        edges = await self._backend.query("spawn_events", order_by="ts", limit=limit)
        return _build_spawn_tree(edges, root_did)

    async def llm_by_identity(self, window: str = "24h") -> dict[str, Any]:
        """Per-identity LLM cost/count — parent vs each child (FR-4 / UC-3)."""
        rows = await self._llm_rows_in_window(window)
        return compute_llm_by_identity(rows, window=window)

    # -- SPEC-054 skill version surfaces (REQ-120) --------------------------

    async def skill_versions(self, skill_name: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Metadata-only version timeline for one skill, ordered by generation."""
        await self._ensure()
        return await store_query.skill_versions(self._backend, skill_name, limit=limit)

    async def skill_candidate_body(self, skill_name: str, candidate_id: str) -> str | None:
        """Full candidate text, or ``None`` when the body is pending/pruned."""
        await self._ensure()
        return await store_query.skill_candidate_body(self._backend, skill_name, candidate_id)
