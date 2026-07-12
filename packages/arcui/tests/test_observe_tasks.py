"""``Observe.tasks`` — arcui reads task rows from the arcstore mutable plane.

SPEC-056 Phase D (D1/D7). PLAN's Best Practices note: "Re-point the read
endpoints by adding an ``Observe.tasks()`` reader mirroring ``Observe.audit``
(observe.py:207-213)." Unlike ``audit``/``traces`` (which come from the
WORM/spool ingest tail), tasks live on the mutable directory plane
(SPEC-056 Phase A, ``arcstore.tasks.TaskStore``) — the SAME ``store/arcui.db``
that ``arcagent.modules.tasks.store.open_store`` writes to (store.py:19-22),
so the reader queries the mutable plane directly; no ingest hop needed.

Also covers the FR-12 activity-timeline dependency: ``Observe.audit`` gains a
``target`` filter so the task drawer can pull ``target == task_id`` history.

Neither ``Observe.tasks`` nor ``Observe.audit(target=...)`` exist yet — RED.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.sqlite import SqliteBackend
from arcstore.tasks import Task, TaskStore

from arcui.observe import Observe

_CREATOR = "did:arc:test:human/operator"


async def _seed_store(data_dir: Path) -> TaskStore:
    """Open a TaskStore against the SAME db Observe reads (store/arcui.db).

    Mirrors ``arcagent.modules.tasks.store.open_store`` exactly — production
    wires the agent-side store and arcui's Observe against the identical
    file so writes are visible without any push wire (SPEC-026 FR-5 spirit
    applied to the mutable plane).
    """
    backend = SqliteBackend(data_dir / "store" / "arcui.db")
    await backend.start()
    return TaskStore(backend)


def _task(id_: str, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "id": id_,
        "title": f"Task {id_}",
        "creator_did": _CREATOR,
    }
    fields.update(overrides)
    return Task(**fields)


@pytest.mark.asyncio
async def test_tasks_returns_rows_from_mutable_plane(tmp_path: Path) -> None:
    store = await _seed_store(tmp_path)
    await store.create(_task("t1", owner_did="did:arc:acme:analyst/aaaa"))
    await store.create(_task("t2"))

    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        rows = await observe.tasks()
        assert {r["id"] for r in rows} == {"t1", "t2"}
        t1 = next(r for r in rows if r["id"] == "t1")
        assert t1["owner_did"] == "did:arc:acme:analyst/aaaa"
        assert t1["status"] == "todo"
        t2 = next(r for r in rows if r["id"] == "t2")
        assert t2["owner_did"] is None
        assert t2["status"] == "backlog"
    finally:
        await observe.stop()


@pytest.mark.asyncio
async def test_tasks_filters_by_owner_did(tmp_path: Path) -> None:
    store = await _seed_store(tmp_path)
    await store.create(_task("t1", owner_did="did:arc:acme:analyst/aaaa"))
    await store.create(_task("t2", owner_did="did:arc:acme:analyst/bbbb"))

    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        rows = await observe.tasks(owner_did="did:arc:acme:analyst/aaaa")
        assert [r["id"] for r in rows] == ["t1"]
    finally:
        await observe.stop()


@pytest.mark.asyncio
async def test_tasks_filters_by_status(tmp_path: Path) -> None:
    store = await _seed_store(tmp_path)
    await store.create(_task("t1", owner_did="did:arc:acme:analyst/aaaa", status="in_progress"))
    await store.create(_task("t2"))  # defaults to backlog (unowned)

    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        rows = await observe.tasks(status="in_progress")
        assert [r["id"] for r in rows] == ["t1"]
    finally:
        await observe.stop()


@pytest.mark.asyncio
async def test_tasks_exposes_run_id_for_cost_link(tmp_path: Path) -> None:
    """FR-11 — run/cost link: the row surfaces ``run_id`` so a task card can
    join to the existing run/trace view. ``observe.timeline`` already joins
    on ``request_id == run_id`` (observe.py:277-291); this only proves the
    field rides through the tasks reader, not the join itself."""
    store = await _seed_store(tmp_path)
    await store.create(
        _task(
            "t1",
            owner_did="did:arc:acme:analyst/aaaa",
            status="in_progress",
            run_id="run-42",
        )
    )

    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        rows = await observe.tasks()
        assert rows[0]["run_id"] == "run-42"
    finally:
        await observe.stop()


def _write_audit_event(
    data_dir: Path, *, seq: int, actor_did: str, action: str, target: str
) -> None:
    """Append one signed-chain record to the durable WORM file arcstore mirrors.

    Mirrors ``test_observe.py::_write_audit`` but parameterizes ``action`` and
    ``target`` — this file needs distinct targets (task ids) to prove the
    filter, which the shared helper doesn't expose.
    """
    worm = data_dir / "worm"
    worm.mkdir(parents=True, exist_ok=True)
    line = {
        "seq": seq,
        "event_hash": f"hash-{seq}",
        "prev_hash": f"hash-{seq - 1}" if seq else "",
        "signature": "sig",
        "event": {
            "ts": f"2026-05-31T00:00:0{seq}+00:00",
            "actor_did": actor_did,
            "action": action,
            "target": target,
            "outcome": "allow",
        },
    }
    with (worm / "audit-chain.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


@pytest.mark.asyncio
async def test_audit_filters_by_target_for_task_activity_timeline(tmp_path: Path) -> None:
    """FR-12 — the task drawer's activity timeline pulls ``observe.audit``
    filtered by ``target == task_id`` (SDD §6). Two tasks' events interleave
    in the chain; only ``task:t1``'s rows must come back, newest first."""
    _write_audit_event(
        tmp_path, seq=0, actor_did="did:arc:x/aaaa", action="tasks.create", target="task:t1"
    )
    _write_audit_event(
        tmp_path, seq=1, actor_did="did:arc:x/bbbb", action="tasks.create", target="task:t2"
    )
    _write_audit_event(
        tmp_path, seq=2, actor_did="did:arc:x/aaaa", action="tasks.start", target="task:t1"
    )

    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        timeline = await observe.audit(target="task:t1")
        assert [e["action"] for e in timeline] == ["tasks.start", "tasks.create"]
        assert all(e["target"] == "task:t1" for e in timeline)
    finally:
        await observe.stop()
