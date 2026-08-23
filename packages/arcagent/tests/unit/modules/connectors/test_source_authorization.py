"""Background connector sources retain grant authorization and audit on every read."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arctrust.audit import AuditEvent

from arcagent.extension.grants import Connection, ConnectionRegistry
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceContent,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SyncSource,
    SyncSourcePage,
)
from arcagent.modules.connected_data.service import SourceOperationResult
from arcagent.modules.connectors.capabilities import _revoke_connected_source, _source_grant_active
from arcagent.modules.connectors.source_authorization import SourceAuthorizationBinding


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _Datastore:
    async def introspect(self) -> str:
        return "schema"

    async def persist_ontology(self, store: Any) -> None:
        del store

    async def query(self, op: str, table: str, args: dict[str, object]) -> object:
        return {"op": op, "table": table, "args": args}


class _Source:
    def __init__(self) -> None:
        self.datastore = _Datastore()

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="postgres",
            account_id="account",
        )

    async def list_source_resources(self, request: ListSourceResources) -> tuple[Any, ...]:
        del request
        return ()

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        del request

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        return SyncSourcePage(next_checkpoint=request.checkpoint or "cursor")

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"authorized",
        )

    async def datastore_port(self) -> _Datastore:
        return self.datastore

    async def close_source(self) -> None: ...


async def test_source_reads_fail_closed_immediately_after_grant_revocation() -> None:
    granted = True
    sink = _Sink()

    async def grant_active() -> bool:
        return granted

    source = SourceAuthorizationBinding(
        _Source(),
        connection_id="database",
        agent_did="did:arc:test:agent",
        tier="federal",
        grant_active=grant_active,
        audit_sink=sink,
    )
    request = InspectSource(connection_id="database")

    assert (await source.inspect_source(request)).source_kind == "postgres"
    granted = False
    with pytest.raises(SourceError) as denied:
        await source.inspect_source(request)

    assert denied.value.code is SourceFailureCode.AUTH_REQUIRED
    assert [(event.action, event.outcome) for event in sink.events] == [
        ("connector.source.inspect", "allow"),
        ("connector.source.inspect", "deny"),
    ]
    assert all(event.actor_did == "did:arc:test:agent" for event in sink.events)


async def test_source_binding_refuses_confused_deputy_connection_id() -> None:
    sink = _Sink()

    async def grant_active() -> bool:
        return True

    source = SourceAuthorizationBinding(
        _Source(),
        connection_id="dropbox-primary",
        agent_did="did:arc:test:agent",
        tier="personal",
        grant_active=grant_active,
        audit_sink=sink,
    )

    with pytest.raises(SourceError) as denied:
        await source.sync_source(SyncSource(connection_id="dropbox-other"))

    assert denied.value.code is SourceFailureCode.AUTH_REQUIRED
    assert sink.events[-1].outcome == "deny"
    assert sink.events[-1].extra["reason"] == "connection_mismatch"


async def test_returned_datastore_port_rechecks_grant_for_every_query() -> None:
    granted = True
    sink = _Sink()

    async def grant_active() -> bool:
        return granted

    source = SourceAuthorizationBinding(
        _Source(),
        connection_id="database",
        agent_did="did:arc:test:agent",
        tier="enterprise",
        grant_active=grant_active,
        audit_sink=sink,
    )
    datastore = await source.datastore_port()
    assert await datastore.query("list", "items", {"limit": 1}) == {
        "op": "list",
        "table": "items",
        "args": {"limit": 1},
    }

    granted = False
    with pytest.raises(SourceError) as denied:
        await datastore.query("list", "items", {})

    assert denied.value.code is SourceFailureCode.AUTH_REQUIRED
    assert [event.action for event in sink.events] == [
        "connector.source.datastore",
        "connector.source.datastore_query",
        "connector.source.datastore_query",
    ]
    assert sink.events[-1].outcome == "deny"


async def test_source_result_is_discarded_when_grant_changes_during_provider_call() -> None:
    checks = iter((True, False))
    sink = _Sink()

    async def grant_active() -> bool:
        return next(checks)

    source = SourceAuthorizationBinding(
        _Source(),
        connection_id="database",
        agent_did="did:arc:test:agent",
        tier="federal",
        grant_active=grant_active,
        audit_sink=sink,
    )

    with pytest.raises(SourceError) as denied:
        await source.inspect_source(InspectSource(connection_id="database"))

    assert denied.value.code is SourceFailureCode.AUTH_REQUIRED
    assert sink.events[-1].outcome == "deny"
    assert sink.events[-1].extra["reason"] == "grant_revoked_during_call"


async def test_runtime_grant_check_reads_current_operator_registry(tmp_path: Path) -> None:
    arc_dir = tmp_path / "arc"
    registry = ConnectionRegistry(arc_dir)
    registry.define(
        "dropbox_primary",
        Connection(extension="dropbox", agents=("olivia",)),
    )
    state = SimpleNamespace(arc_dir=arc_dir, agent_dir=tmp_path / "olivia")

    assert await _source_grant_active(state, "dropbox_primary")
    registry.revoke("dropbox_primary", ["olivia"])
    assert not await _source_grant_active(state, "dropbox_primary")


@pytest.mark.asyncio
async def test_grant_reconcile_purges_connected_knowledge_before_unregistering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revoke has to purge indexed data, not merely deny future provider reads."""
    calls: list[str] = []

    class _Service:
        async def revoke(self, connection_id: str) -> SourceOperationResult:
            calls.append(connection_id)
            return SourceOperationResult(connection_id, "revoked")

    from arcagent.modules.connected_data import _runtime as connected_runtime

    monkeypatch.setattr(connected_runtime, "state", lambda: SimpleNamespace(service=_Service()))
    state = SimpleNamespace(
        identity=SimpleNamespace(did="did:arc:test:agent"), tier="personal", telemetry=None
    )

    await _revoke_connected_source(state, "dropbox-primary")

    assert calls == ["dropbox-primary"]
