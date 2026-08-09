"""ConnectionStateStore — per-connection operational state (SPEC-062 COMP-019).

Covers REQ-295 (health, last successful use, credential expiry recorded in the
operational data plane so surfaces report status without probing) and REQ-291
(approved tool-contract hashes per (connection, tool) — the rug-pull defence's
read/write side).

Every test drives the REAL SQLite mutable plane, never a mock of it, so a
store that looks correct against a fake but not against the durable path
cannot pass. Three failure modes this project has actually been bitten by get
their own tests:

* Read-modify-write updates — ``test_every_mutation_is_a_single_backend_call``
  records EVERY backend call, so a store that reads a snapshot and writes it
  back (the shape that loses a concurrent writer's field, or leaves a record
  built from stale state after a crash) fails even though its result looks
  right in a single-threaded test.
* [[feedback_concurrency_tests_must_interleave]] — the concurrent-approval test
  forces both writers to reach their write at the same instant with an
  ``asyncio.Barrier``; an instant mock would let ``gather`` run them
  sequentially and the race would never fire.
* [[project_scheduler_store_poison_stops_engine]] — one malformed row must not
  silently take out every other connection's status.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from arcstore.backends.sqlite import SqliteBackend
from arcstore.config import store_db_path
from pydantic import ValidationError

if TYPE_CHECKING:
    from arcagent.extension.state import ConnectionStateStore

_ACTOR = "did:arc:test:human/operator"
_CONNECTION = "jira_primary"

# Any of these appearing as a field name would mean the schema can hold a
# credential VALUE. REQ-265/REQ-277: coordinates (issuer, audience, expiry)
# yes, values never.
_SECRET_WORDS = ("token", "secret", "password", "api_key", "private_key", "credential_value")


class _RecordingSink:
    """Minimal in-memory AuditSink — satisfies the ``write(event)`` Protocol."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def write(self, event: Any) -> None:
        self.events.append(event)


class _CountingBackend:
    """Delegates to a real backend, recording EVERY call per store operation.

    Reads are recorded too: a read-modify-write update is exactly what this
    store must never do, and counting only the writes would not see it.
    """

    def __init__(self, inner: SqliteBackend) -> None:
        self._inner = inner
        self.calls: list[str] = []

    async def mutable_create_batch(
        self,
        collection: str,
        entries: Sequence[tuple[str, dict[str, Any]]],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append("create")
        return await self._inner.mutable_create_batch(
            collection, entries, actor_did=actor_did, sink=sink
        )

    async def mutable_merge(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool:
        self.calls.append("merge")
        return await self._inner.mutable_merge(
            collection, key, patch, actor_did=actor_did, sink=sink
        )

    async def mutable_delete(
        self, collection: str, key: str, *, actor_did: str, sink: Any | None = None
    ) -> bool:
        self.calls.append("delete")
        return await self._inner.mutable_delete(collection, key, actor_did=actor_did, sink=sink)

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None:
        self.calls.append("read")
        return await self._inner.mutable_read(collection, key)

    async def mutable_query(
        self, collection: str, *, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append("query")
        return await self._inner.mutable_query(collection, where=where)


class _BarrierBackend:
    """Real backend whose merges all wait on one barrier — forces interleaving."""

    def __init__(self, inner: SqliteBackend, barrier: asyncio.Barrier) -> None:
        self._inner = inner
        self._barrier = barrier

    async def mutable_create_batch(
        self,
        collection: str,
        entries: Sequence[tuple[str, dict[str, Any]]],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> list[dict[str, Any]]:
        return await self._inner.mutable_create_batch(
            collection, entries, actor_did=actor_did, sink=sink
        )

    async def mutable_merge(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool:
        await self._barrier.wait()
        return await self._inner.mutable_merge(
            collection, key, patch, actor_did=actor_did, sink=sink
        )

    async def mutable_delete(
        self, collection: str, key: str, *, actor_did: str, sink: Any | None = None
    ) -> bool:
        return await self._inner.mutable_delete(collection, key, actor_did=actor_did, sink=sink)

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None:
        return await self._inner.mutable_read(collection, key)

    async def mutable_query(
        self, collection: str, *, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return await self._inner.mutable_query(collection, where=where)


@pytest.fixture
async def backend(tmp_path: Path) -> SqliteBackend:
    """A db file of this test's own — never the resolved shared store.

    Deliberately NOT ``store_db_path(tmp_path)``: ``ARCSTORE_DATA_DIR`` outranks
    that argument in ``resolve_data_dir``, so the path would come from the
    environment and these assertions would depend on the shell they run in. A
    row leaked in from another test or another run would then be
    indistinguishable from a bug in ``list()``.
    """
    inner = SqliteBackend(tmp_path / "connection-state.db")
    await inner.start()
    return inner


@pytest.fixture
async def store(backend: SqliteBackend) -> ConnectionStateStore:
    from arcagent.extension.state import ConnectionStateStore

    return ConnectionStateStore(backend)


async def _create(store: ConnectionStateStore, *, connection: str = _CONNECTION) -> Any:
    from arcagent.extension.state import ConnectionRecord

    return await store.create(ConnectionRecord(connection=connection), actor_did=_ACTOR)


# --- schema: coordinates, never values (REQ-265, REQ-277) -------------------


def test_record_rejects_an_unmodeled_credential_field() -> None:
    from arcagent.extension.state import ConnectionRecord

    with pytest.raises(ValidationError):
        ConnectionRecord(
            connection=_CONNECTION,
            access_token="sekrit",  # type: ignore[call-arg]  # reason: unmodeled key
        )


def test_no_field_can_hold_a_credential_value() -> None:
    from arcagent.extension.state import ConnectionRecord

    offenders = [
        name
        for name in ConnectionRecord.model_fields
        if any(word in name for word in _SECRET_WORDS)
    ]
    assert offenders == []
    # No unmodeled key can be smuggled in either — the guard above only holds
    # while the schema is closed.
    assert ConnectionRecord.model_config["extra"] == "forbid"


async def test_persisted_row_carries_only_schema_keys(
    store: ConnectionStateStore, backend: SqliteBackend
) -> None:
    from arcagent.extension.state import CONNECTION_COLLECTION, ConnectionRecord

    await _create(store)
    row = await backend.mutable_read(CONNECTION_COLLECTION, _CONNECTION)

    assert row is not None
    assert set(row) <= set(ConnectionRecord.model_fields)


# --- REQ-295: health, last use, credential expiry ---------------------------


async def test_create_then_get_round_trips(store: ConnectionStateStore) -> None:
    created = await _create(store)
    fetched = await store.get(_CONNECTION)

    assert fetched is not None
    assert fetched.connection == _CONNECTION
    assert fetched.health == "unknown"
    assert created.created_at is not None


async def test_recreating_a_connection_keeps_its_approved_hashes(
    store: ConnectionStateStore,
) -> None:
    """A repeated install must not silently discard what the operator approved."""
    await _create(store)
    await store.approve_tool_contract(_CONNECTION, "create_issue", "sha256:aaa", actor_did=_ACTOR)

    await _create(store)

    assert await store.approved_hash(_CONNECTION, "create_issue") == "sha256:aaa"


async def test_get_returns_none_for_unknown_connection(store: ConnectionStateStore) -> None:
    assert await store.get("never_installed") is None


async def test_mark_healthy_records_health_and_last_successful_use(
    store: ConnectionStateStore,
) -> None:
    await _create(store)

    assert await store.mark_healthy(_CONNECTION, actor_did=_ACTOR) is True
    fetched = await store.get(_CONNECTION)

    assert fetched is not None
    assert fetched.health == "healthy"
    assert fetched.last_success_at is not None


async def test_set_health_marks_a_connection_needing_attention(
    store: ConnectionStateStore,
) -> None:
    await _create(store)

    await store.set_health(_CONNECTION, "needs_attention", actor_did=_ACTOR)
    fetched = await store.get(_CONNECTION)

    assert fetched is not None
    assert fetched.health == "needs_attention"


async def test_credential_metadata_records_coordinates_not_the_value(
    store: ConnectionStateStore, backend: SqliteBackend
) -> None:
    from arcagent.extension.state import CONNECTION_COLLECTION

    await _create(store)
    await store.record_credential_metadata(
        _CONNECTION,
        expires_at="2026-09-01T00:00:00+00:00",
        issuer="https://auth.example.test",
        audience="api://jira",
        actor_did=_ACTOR,
    )

    fetched = await store.get(_CONNECTION)
    assert fetched is not None
    assert fetched.credential_expires_at == "2026-09-01T00:00:00+00:00"
    assert fetched.credential_issuer == "https://auth.example.test"
    assert fetched.credential_audience == "api://jira"

    row = await backend.mutable_read(CONNECTION_COLLECTION, _CONNECTION)
    assert row is not None
    assert not any(word in str(row).lower() for word in ("ghp_", "bearer ", "refresh_token"))


async def test_credential_last_refresh_is_recorded(store: ConnectionStateStore) -> None:
    """COMP-011 renews against expiry, but needs its own last-refresh mark."""
    await _create(store)

    await store.record_credential_metadata(
        _CONNECTION, last_refresh_at="2026-08-04T09:00:00+00:00", actor_did=_ACTOR
    )
    fetched = await store.get(_CONNECTION)

    assert fetched is not None
    assert fetched.credential_last_refresh_at == "2026-08-04T09:00:00+00:00"


async def test_last_refresh_survives_unrelated_activity(store: ConnectionStateStore) -> None:
    """``updated_at`` is not a substitute — any write moves it, a refresh does not."""
    await _create(store)
    await store.record_credential_metadata(
        _CONNECTION, last_refresh_at="2026-08-04T09:00:00+00:00", actor_did=_ACTOR
    )

    await store.mark_healthy(_CONNECTION, actor_did=_ACTOR)
    fetched = await store.get(_CONNECTION)

    assert fetched is not None
    assert fetched.credential_last_refresh_at == "2026-08-04T09:00:00+00:00"
    assert fetched.updated_at != fetched.credential_last_refresh_at


async def test_list_reports_every_connection_in_the_deployment(
    store: ConnectionStateStore,
) -> None:
    """One deployment, one list. A connection is not an agent's, so it is not filtered
    by one — the grant list in ``connections.toml`` is what says who reaches it."""
    await _create(store, connection="jira_primary")
    await _create(store, connection="gmail_primary")

    names = sorted(record.connection for record in await store.list())

    assert names == ["gmail_primary", "jira_primary"]


async def test_updating_a_missing_connection_creates_nothing(
    store: ConnectionStateStore,
) -> None:
    assert await store.mark_healthy("never_installed", actor_did=_ACTOR) is False
    assert await store.get("never_installed") is None


async def test_forget_removes_the_connection(store: ConnectionStateStore) -> None:
    await _create(store)

    assert await store.forget(_CONNECTION, actor_did=_ACTOR) is True
    assert await store.get(_CONNECTION) is None


# --- REQ-291: approved tool-contract hashes ---------------------------------


async def test_approved_hash_round_trips_per_tool(store: ConnectionStateStore) -> None:
    await _create(store)

    await store.approve_tool_contract(_CONNECTION, "create_issue", "sha256:aaa", actor_did=_ACTOR)

    assert await store.approved_hash(_CONNECTION, "create_issue") == "sha256:aaa"
    assert await store.approved_hash(_CONNECTION, "delete_issue") is None


async def test_reapproving_a_tool_replaces_its_hash(store: ConnectionStateStore) -> None:
    await _create(store)

    await store.approve_tool_contract(_CONNECTION, "create_issue", "sha256:aaa", actor_did=_ACTOR)
    await store.approve_tool_contract(_CONNECTION, "create_issue", "sha256:bbb", actor_did=_ACTOR)

    assert await store.approved_hash(_CONNECTION, "create_issue") == "sha256:bbb"


async def test_concurrent_approvals_of_two_tools_both_persist(
    backend: SqliteBackend,
) -> None:
    """Forced interleaving: a read-modify-write store loses one approval here."""
    from arcagent.extension.state import ConnectionStateStore

    seed = ConnectionStateStore(backend)
    await _create(seed)

    barrier = asyncio.Barrier(2)
    racing = ConnectionStateStore(_BarrierBackend(backend, barrier))
    await asyncio.gather(
        racing.approve_tool_contract(_CONNECTION, "create_issue", "sha256:aaa", actor_did=_ACTOR),
        racing.approve_tool_contract(_CONNECTION, "add_comment", "sha256:bbb", actor_did=_ACTOR),
    )

    assert await seed.approved_hash(_CONNECTION, "create_issue") == "sha256:aaa"
    assert await seed.approved_hash(_CONNECTION, "add_comment") == "sha256:bbb"


# --- dependency declarations (reference-counted removal, later) -------------


async def test_dependency_declarations_round_trip(store: ConnectionStateStore) -> None:
    await _create(store)

    await store.declare_dependencies(
        _CONNECTION, ["httpx>=0.27", "atlassian-api"], actor_did=_ACTOR
    )
    fetched = await store.get(_CONNECTION)

    assert fetched is not None
    assert fetched.dependency_declarations == ["httpx>=0.27", "atlassian-api"]


# --- atomicity: one statement per mutation, never read-modify-write ---------


async def test_every_mutation_is_a_single_backend_call(backend: SqliteBackend) -> None:
    """A half-written record is impossible only if each update is one statement.

    One call, and that call a merge: no read-modify-write (which would let a
    crash or a concurrent writer land a record built from a stale snapshot),
    and no full-row rewrite (which would clobber fields it never read).
    """
    from arcagent.extension.state import ConnectionStateStore

    counting = _CountingBackend(backend)
    store = ConnectionStateStore(counting)
    await _create(store)

    operations: tuple[Callable[[], Awaitable[bool]], ...] = (
        lambda: store.mark_healthy(_CONNECTION, actor_did=_ACTOR),
        lambda: store.set_health(_CONNECTION, "degraded", actor_did=_ACTOR),
        lambda: store.approve_tool_contract(
            _CONNECTION, "create_issue", "sha256:aaa", actor_did=_ACTOR
        ),
        lambda: store.declare_dependencies(_CONNECTION, ["httpx>=0.27"], actor_did=_ACTOR),
    )
    for operation in operations:
        counting.calls.clear()
        await operation()
        assert counting.calls == ["merge"]


async def test_writes_carry_the_actor_to_the_audit_sink(backend: SqliteBackend) -> None:
    from arcagent.extension.state import ConnectionStateStore

    sink = _RecordingSink()
    store = ConnectionStateStore(backend, sink=sink)
    await _create(store)

    assert sink.events


# --- poison: one bad row must not take out the whole surface ---------------


async def test_get_on_an_unreadable_row_raises_naming_the_connection(
    store: ConnectionStateStore, backend: SqliteBackend
) -> None:
    from arcagent.core.errors import ExtensionError
    from arcagent.extension.state import CONNECTION_COLLECTION

    await backend.mutable_write(
        CONNECTION_COLLECTION,
        _CONNECTION,
        {"not": "a connection record"},
        actor_did=_ACTOR,
    )

    with pytest.raises(ExtensionError) as excinfo:
        await store.get(_CONNECTION)

    assert _CONNECTION in str(excinfo.value)


async def test_list_survives_one_unreadable_row_and_logs_it(
    store: ConnectionStateStore,
    backend: SqliteBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from arcagent.extension.state import CONNECTION_COLLECTION

    await _create(store, connection="jira_primary")
    await _create(store, connection="gmail_primary")
    # Schema drift on one row — the shape a poisoned record actually takes.
    await backend.mutable_write(
        CONNECTION_COLLECTION,
        "poisoned",
        {"connection": "poisoned", "health": "on fire"},
        actor_did=_ACTOR,
    )

    with caplog.at_level(logging.ERROR):
        records = await store.list()

    # Every healthy row still reported, the bad one excluded and named in the
    # log — one corrupt record must never blind a surface to the rest.
    assert sorted(r.connection for r in records) == ["gmail_primary", "jira_primary"]
    assert "poisoned" not in {r.connection for r in records}
    assert any("poisoned" in message for message in caplog.messages)


# --- opener -----------------------------------------------------------------


async def test_open_connection_state_uses_the_shared_operational_db(tmp_path: Path) -> None:
    """The opener lands on the resolved store — the db arcui and the CLI read.

    The repo's autouse ``_isolate_arcstore_data_dir`` fixture points
    ``ARCSTORE_DATA_DIR`` at this test's tmp dir, and that env var is the top of
    ``resolve_data_dir``'s precedence — so the resolved path is asserted here,
    not a path built from ``tmp_path``, which the env would override anyway.
    """
    from arcagent.extension.state import open_connection_state

    store = await open_connection_state(str(tmp_path))
    await _create(store)

    assert store_db_path().exists()
    assert await store.get(_CONNECTION) is not None
