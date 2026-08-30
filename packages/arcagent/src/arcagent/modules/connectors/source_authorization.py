"""Per-read grant authorization and audit for connected source adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceAdapter,
    SourceContent,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceResource,
    SyncSource,
    SyncSourcePage,
)

_T = TypeVar("_T")
_GrantCheck = Callable[[], Awaitable[bool]]


class SourceAuthorizationBinding:
    """Bind one raw source adapter to an agent's current deployment grant.

    A connector reconcile is eventual, while revocation is an authorization
    event that must take effect immediately. Every source read therefore checks
    the deployment grant again before touching the provider and emits one audit
    verdict without recording locators, object identifiers, or source content.
    """

    def __init__(
        self,
        source: SourceAdapter,
        *,
        connection_id: str,
        agent_did: str,
        tier: str,
        grant_active: _GrantCheck,
        audit_sink: AuditSink,
    ) -> None:
        self._source = source
        self._connection_id = connection_id
        self._agent_did = agent_did
        self._tier = tier
        self._grant_active = grant_active
        self._audit_sink = audit_sink

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return await self._authorized(
            "inspect",
            request.connection_id,
            lambda: self._source.inspect_source(request),
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        return await self._authorized(
            "list_resources",
            request.connection_id,
            lambda: self._source.list_source_resources(request),
        )

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        await self._authorized(
            "select_resources",
            request.connection_id,
            lambda: self._source.select_source_resources(request),
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        return await self._authorized(
            "sync",
            request.connection_id,
            lambda: self._source.sync_source(request),
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return await self._authorized(
            "fetch",
            request.connection_id,
            lambda: self._source.fetch_source(request),
        )

    async def datastore_port(self) -> _AuthorizedDatastorePort:
        get_port = getattr(self._source, "datastore_port", None)
        if not callable(get_port):
            raise SourceError(
                SourceFailureCode.UNSUPPORTED_CONTENT,
                "connected source has no datastore port",
            )
        datastore = await self._authorized(
            "datastore",
            self._connection_id,
            get_port,
        )
        return _AuthorizedDatastorePort(self, datastore)

    async def close_source(self) -> None:
        """Always release the adapter, including after its grant is revoked."""
        await self._source.close_source()

    async def _authorized(
        self,
        operation: str,
        request_connection_id: str,
        call: Callable[[], Awaitable[_T]],
    ) -> _T:
        if request_connection_id != self._connection_id:
            self._audit(operation, "deny", reason="connection_mismatch")
            raise SourceError(
                SourceFailureCode.AUTH_REQUIRED,
                "source request does not match the granted connection",
            )
        if not await self._grant_is_active(operation):
            self._audit(operation, "deny", reason="grant_revoked")
            raise SourceError(SourceFailureCode.AUTH_REQUIRED, "source grant is not active")
        try:
            result = await call()
        except Exception:
            self._audit(operation, "error", reason="source_error")
            raise
        if not await self._grant_is_active(operation):
            self._audit(operation, "deny", reason="grant_revoked_during_call")
            raise SourceError(SourceFailureCode.AUTH_REQUIRED, "source grant is not active")
        self._audit(operation, "allow")
        return result

    async def _grant_is_active(self, operation: str) -> bool:
        try:
            return await self._grant_active()
        except Exception as exc:
            self._audit(operation, "deny", reason="grant_check_failed")
            raise SourceError(
                SourceFailureCode.AUTH_REQUIRED,
                "source grant could not be verified",
            ) from exc

    def _audit(self, operation: str, outcome: str, *, reason: str = "") -> None:
        emit(
            AuditEvent(
                actor_did=self._agent_did,
                action=f"connector.source.{operation}",
                target=f"connector:{self._connection_id}",
                outcome=outcome,
                tier=self._tier,
                extra={"reason": reason} if reason else {},
            ),
            self._audit_sink,
        )


class _AuthorizedDatastorePort:
    """Keep live datastore reads subject to the source grant after registration."""

    def __init__(self, source: SourceAuthorizationBinding, datastore: Any) -> None:
        self._source = source
        self._datastore = datastore

    async def introspect(self, *, sample_limit: int = 0) -> Any:
        return await self._source._authorized(
            "datastore_introspect",
            self._source._connection_id,
            lambda: self._datastore.introspect(sample_limit=sample_limit),
        )

    async def persist_ontology(self, store: Any) -> None:
        await self._source._authorized(
            "datastore_ontology",
            self._source._connection_id,
            lambda: self._datastore.persist_ontology(store),
        )

    async def query(self, op: str, table: str, args: dict[str, object]) -> object:
        return await self._source._authorized(
            "datastore_query",
            self._source._connection_id,
            lambda: self._datastore.query(op, table, args),
        )


__all__ = ["SourceAuthorizationBinding"]
