"""SPEC-088 T-1335 (RED): ``WorkEvent`` and ``WorkEventLog`` (COMP-012).

Contract under test (``arcstore.work_events``, new module):

* ``WorkEvent`` is frozen, ``extra="forbid"``, with ``kind`` limited to
  ``run.finished | run.failed | gate.waiting | human_input.waiting |
  gate.escalated``. ``seq`` is assigned by the store.
* ``WorkEventLog(backend, *, clock=None, lease_seconds=60.0)``. ``clock``
  returns a timezone-aware ``datetime``; the log passes its ``now`` to the
  backend so leases and retry delays are deterministic on every backend.
* ``append(event) -> bool`` inserts once per ``event_id`` (True, then False).
  An event with ``callback_url`` gets exactly one ``pending`` delivery row in
  the same write.
* ``read(after_seq, *, limit, where=None) -> list[WorkEvent]`` returns events
  with ``seq > after_seq`` in strictly increasing ``seq`` order. ``where`` is
  field equality (used here to isolate a run on a shared PostgreSQL database).
* ``claim_deliveries(consumer, limit)`` leases due ``pending`` deliveries; each
  item exposes ``event_id`` and ``callback_url``.
* ``settle(event_id, outcome=..., retry_after_s=..., error=...)``; outcome
  ``retry`` puts the delivery back to ``pending`` hidden until
  ``now + retry_after_s``; ``delivered`` ends it.

Every store test runs on the memory backend and on PostgreSQL (same DSN gate as
``conftest.postgres_backend``: skipped without ``ARCSTORE_TEST_DATABASE_URL``).
Ids are unique per test so a shared database never leaks across runs.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from arcstore.backends.base import ArcStoreBackend
from arcstore.backends.memory import FakeBackend
from arcstore.backends.postgres import PostgresBackend
from arcstore.config import ArcStoreConfig

_AGENT = "did:arc:test:events/aaaaaaaa"
_OTHER_AGENT = "did:arc:test:events/bbbbbbbb"
_CALLBACK = "https://hooks.example.test/arc"
_KINDS = ("run.finished", "run.failed", "gate.waiting", "human_input.waiting", "gate.escalated")


class _Clock:
    """A settable, timezone-aware clock injected into ``WorkEventLog``."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(params=["memory", "postgres"])
async def backend(request: pytest.FixtureRequest) -> AsyncIterator[ArcStoreBackend]:
    """The memory backend, and a PostgreSQL twin that skips without a test DSN."""
    if request.param == "postgres":
        dsn = os.environ.get("ARCSTORE_TEST_DATABASE_URL")
        if not dsn:
            pytest.skip("ARCSTORE_TEST_DATABASE_URL is required for PostgreSQL integration tests")
        real: ArcStoreBackend = PostgresBackend(ArcStoreConfig().postgres_settings(SecretStr(dsn)))
    else:
        real = FakeBackend()
    await real.start()
    try:
        yield real
    finally:
        await real.stop()


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _log(backend: Any, clock: _Clock, *, lease_seconds: float = 60.0) -> Any:
    from arcstore.work_events import WorkEventLog

    return WorkEventLog(backend, clock=clock, lease_seconds=lease_seconds)


def _event(run_id: str, **fields: Any) -> Any:
    from arcstore.work_events import WorkEvent

    payload: dict[str, Any] = {
        "event_id": f"evt-{uuid.uuid4().hex}",
        "kind": "run.finished",
        "occurred_at": "2026-10-10T12:00:00+00:00",
        "agent_did": _AGENT,
        "workflow_id": None,
        "run_id": run_id,
        "node_id": None,
        "task_id": f"intake-{run_id}",
        "correlation_id": "order-42",
        "status": "done",
        "classification": "UNCLASSIFIED",
        "callback_url": None,
    }
    payload.update(fields)
    return WorkEvent(**payload)


def _run() -> str:
    return f"run-{uuid.uuid4().hex}"


async def _claimed_ids(log: Any, consumer: str) -> list[str]:
    return [item.event_id for item in await log.claim_deliveries(consumer, 100)]


class TestWorkEventModel:
    """The envelope is closed: no extra fields, no mutation, a fixed kind set."""

    @pytest.mark.parametrize("kind", _KINDS)
    def test_work_event_accepts_each_declared_kind(self, kind: str) -> None:
        event = _event(_run(), kind=kind)
        assert event.kind == kind
        assert event.seq is None

    def test_work_event_unknown_kind_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="kind"):
            _event(_run(), kind="run.started")

    def test_work_event_extra_field_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="signature"):
            _event(_run(), signature="forged")

    def test_work_event_is_frozen(self) -> None:
        event = _event(_run())
        with pytest.raises(ValidationError):
            event.status = "failed"


class TestWorkEventLogAppend:
    """REQ-623: one row per ``event_id``, however often it is appended."""

    async def test_append_same_event_id_twice_stores_one_row(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        log = _log(backend, clock)
        run_id = _run()
        event = _event(run_id)

        first = await log.append(event)
        second = await log.append(event)

        assert first is True
        assert second is False
        rows = await log.read(0, limit=100, where={"run_id": run_id})
        assert [row.event_id for row in rows] == [event.event_id]

    async def test_append_duplicate_event_id_keeps_the_first_content(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        log = _log(backend, clock)
        run_id = _run()
        original = _event(run_id, status="done")
        rewrite = _event(run_id, event_id=original.event_id, status="failed", kind="run.failed")

        await log.append(original)
        assert await log.append(rewrite) is False

        (row,) = await log.read(0, limit=100, where={"run_id": run_id})
        assert row.status == "done"
        assert row.kind == "run.finished"

    async def test_append_ignores_a_caller_supplied_seq(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        # The order is the store's, never the caller's.
        log = _log(backend, clock)
        run_id = _run()
        forged = _event(run_id, seq=10**12)
        honest = _event(run_id)

        await log.append(forged)
        await log.append(honest)

        rows = await log.read(0, limit=100, where={"run_id": run_id})
        assert [row.event_id for row in rows] == [forged.event_id, honest.event_id]
        assert rows[0].seq != 10**12
        assert rows[0].seq < rows[1].seq


class TestWorkEventLogRead:
    """REQ-624: ``read(after_seq)`` pages in strictly increasing ``seq`` order."""

    async def test_read_returns_strictly_increasing_seq(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        log = _log(backend, clock)
        run_id = _run()
        events = [_event(run_id) for _ in range(5)]
        for event in events:
            await log.append(event)

        rows = await log.read(0, limit=100, where={"run_id": run_id})

        assert [row.event_id for row in rows] == [e.event_id for e in events]
        seqs = [row.seq for row in rows]
        assert all(isinstance(seq, int) for seq in seqs)
        assert all(earlier < later for earlier, later in pairwise(seqs))

    async def test_read_after_seq_resumes_without_gaps_or_repeats(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        log = _log(backend, clock)
        run_id = _run()
        events = [_event(run_id) for _ in range(5)]
        for event in events:
            await log.append(event)

        page_one = await log.read(0, limit=2, where={"run_id": run_id})
        page_two = await log.read(page_one[-1].seq, limit=100, where={"run_id": run_id})

        assert len(page_one) == 2
        assert [row.event_id for row in page_one + page_two] == [e.event_id for e in events]

    async def test_read_after_last_seq_is_empty(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        log = _log(backend, clock)
        run_id = _run()
        await log.append(_event(run_id))
        (row,) = await log.read(0, limit=100, where={"run_id": run_id})

        assert await log.read(row.seq, limit=100, where={"run_id": run_id}) == []

    async def test_read_where_agent_returns_only_that_agents_events(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        # The SSE stream scopes by agent; another agent's events must not leak.
        log = _log(backend, clock)
        run_id = _run()
        mine = _event(run_id, agent_did=_AGENT)
        theirs = _event(run_id, agent_did=_OTHER_AGENT)
        await log.append(mine)
        await log.append(theirs)

        rows = await log.read(0, limit=100, where={"run_id": run_id, "agent_did": _AGENT})

        assert [row.event_id for row in rows] == [mine.event_id]


class TestWorkEventDeliveries:
    """REQ-623: a callback event gets one durable, leased, retryable delivery."""

    async def test_event_with_callback_creates_exactly_one_pending_delivery(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        log = _log(backend, clock)
        event = _event(_run(), callback_url=_CALLBACK)
        await log.append(event)
        await log.append(event)

        claimed = [
            item
            for item in await log.claim_deliveries(f"c-{uuid.uuid4().hex}", 100)
            if item.event_id == event.event_id
        ]

        assert len(claimed) == 1
        assert claimed[0].callback_url == _CALLBACK

    async def test_event_without_callback_creates_no_delivery(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        log = _log(backend, clock)
        event = _event(_run(), callback_url=None)
        await log.append(event)

        assert event.event_id not in await _claimed_ids(log, f"c-{uuid.uuid4().hex}")

    async def test_claimed_delivery_is_leased_from_other_consumers(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        log = _log(backend, clock)
        event = _event(_run(), callback_url=_CALLBACK)
        await log.append(event)

        assert event.event_id in await _claimed_ids(log, f"a-{uuid.uuid4().hex}")
        assert event.event_id not in await _claimed_ids(log, f"b-{uuid.uuid4().hex}")

    async def test_expired_lease_is_reclaimable(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        # A crashed worker must not strand a delivery forever.
        log = _log(backend, clock, lease_seconds=60.0)
        event = _event(_run(), callback_url=_CALLBACK)
        await log.append(event)
        assert event.event_id in await _claimed_ids(log, f"a-{uuid.uuid4().hex}")

        clock.advance(61)

        assert event.event_id in await _claimed_ids(log, f"b-{uuid.uuid4().hex}")

    async def test_settle_retry_hides_delivery_until_next_attempt_at(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        log = _log(backend, clock, lease_seconds=60.0)
        event = _event(_run(), callback_url=_CALLBACK)
        await log.append(event)
        assert event.event_id in await _claimed_ids(log, f"a-{uuid.uuid4().hex}")

        await log.settle(event.event_id, outcome="retry", retry_after_s=300, error="HTTP 503")

        clock.advance(299)
        assert event.event_id not in await _claimed_ids(log, f"b-{uuid.uuid4().hex}")
        clock.advance(2)
        assert event.event_id in await _claimed_ids(log, f"c-{uuid.uuid4().hex}")

    async def test_settle_delivered_is_never_claimed_again(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        log = _log(backend, clock, lease_seconds=60.0)
        event = _event(_run(), callback_url=_CALLBACK)
        await log.append(event)
        assert event.event_id in await _claimed_ids(log, f"a-{uuid.uuid4().hex}")

        await log.settle(event.event_id, outcome="delivered", retry_after_s=None, error=None)

        clock.advance(3600)
        assert event.event_id not in await _claimed_ids(log, f"b-{uuid.uuid4().hex}")

    async def test_claim_respects_limit(self, backend: ArcStoreBackend, clock: _Clock) -> None:
        log = _log(backend, clock)
        for _ in range(3):
            await log.append(_event(_run(), callback_url=_CALLBACK))

        claimed = await log.claim_deliveries(f"c-{uuid.uuid4().hex}", 2)

        assert len(claimed) == 2


class TestWorkEventLogGet:
    """The callback dispatcher loads the event a claimed delivery points at."""

    async def test_get_returns_the_stored_event_with_seq_and_callback(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        log = _log(backend, clock)
        event = _event(_run(), callback_url=_CALLBACK, status="done")
        await log.append(event)
        (delivery,) = [
            d
            for d in await log.claim_deliveries(f"c-{uuid.uuid4().hex}", 100)
            if d.event_id == event.event_id
        ]

        loaded = await log.get(delivery.event_id)

        assert loaded is not None
        assert loaded.event_id == event.event_id
        assert loaded.callback_url == _CALLBACK
        assert loaded.status == "done"
        assert isinstance(loaded.seq, int)

    async def test_get_unknown_event_id_is_none(
        self, backend: ArcStoreBackend, clock: _Clock
    ) -> None:
        log = _log(backend, clock)

        assert await log.get(f"evt-missing-{uuid.uuid4().hex}") is None
