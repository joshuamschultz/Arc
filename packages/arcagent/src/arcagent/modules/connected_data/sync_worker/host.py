"""What the sync worker asks the main process for, and the worker-side proxies that ask.

The worker writes document stores and holds nothing else: no provider
credential, no database credential, no private key. Whatever a write needs
beyond the files it writes, it asks the main process for over the
authenticated ``host`` channel:

* ``sign`` one store seal, with the key the main process has pinned for that
  store (``arcmemory.okf_seal.sign_for`` signs only a well-formed seal payload
  for exactly that collection, so the channel is no general-purpose signer);
* the arcstore rows a write reads or advances: per-object versions, the source
  incarnation, mapping approvals and knowledge subscriptions;
* an ``audit`` write for an event raised outside any request.

Each row operation is checked here, in the main process: an object-state owner
must be an agent this process serves or a connection's knowledge principal, an
approval row may only be listed or created for such an agent, and a created row
must be a mapping proposal.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from importlib import import_module
from pathlib import Path
from typing import Any

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.connected_data import KnowledgeHome
from arcagent.extension.knowledge_subscriptions import (
    KnowledgeSubscription,
    KnowledgeSubscriptions,
    is_knowledge_principal,
)
from arcagent.modules.connected_data.ingest import ArcStoreObjectState
from arcagent.modules.connected_data.sync_worker.rpc import (
    RemoteError,
    RpcClient,
    RpcError,
    RpcUnavailableError,
)
from arcagent.modules.connected_data.sync_worker.specs import SealKey

_logger = logging.getLogger("arcagent.modules.connected_data.sync_worker.host")

#: The only approval a worker may stage: a connected-source mapping proposal.
_MAPPING_TOOL = "memory.map_source"
_DOCUMENT_ONLY = (KnowledgeHome.DOCUMENT,)
#: How long one host call may take before the worker treats the host as gone.
HOST_CALL_TIMEOUT = 30.0


class HostService:
    """Answer the worker's ``host`` calls from the main process's own seams."""

    def __init__(
        self,
        *,
        arcstore_opener: Callable[[], Awaitable[Any]] | None,
        audit_sink: AuditSink | None,
        sign: Callable[[Path, bytes], bytes] | None = None,
    ) -> None:
        self._opener = arcstore_opener
        self._backend: Any = None
        self._backend_lock = asyncio.Lock()
        self._audit = audit_sink
        self._sign = sign or _okf_sign
        self._agents: set[str] = set()

    def serve_agent(self, agent_did: str) -> None:
        """An agent of this process whose stores the worker may write."""
        if agent_did:
            self._agents.add(agent_did)

    def bind(
        self,
        *,
        arcstore_opener: Callable[[], Awaitable[Any]] | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        """Fill a seam the service was started without (an agent configured later)."""
        if self._opener is None and arcstore_opener is not None:
            self._opener = arcstore_opener
        if self._audit is None and audit_sink is not None:
            self._audit = audit_sink

    async def handle(self, op: str, args: dict[str, Any], body: bytes) -> tuple[Any, bytes]:
        if op == "sign":
            signature = await self._sign_seal(args, body)
            return None, signature
        if op == "audit":
            self._record(args.get("event"))
            return None, b""
        handler = _ROW_OPS.get(op)
        if handler is None:
            raise RpcError("unknown_op", op)
        return await handler(self, args), b""

    async def _sign_seal(self, args: dict[str, Any], message: bytes) -> bytes:
        root = Path(str(args.get("root", "")))
        try:
            # A Vault-transit key signs over the network: never on this loop.
            return await asyncio.to_thread(self._sign, root, message)
        except PermissionError as exc:
            raise RpcError("refused", str(exc), reason="seal_not_signable") from exc

    def _record(self, raw: Any) -> None:
        if self._audit is None or not isinstance(raw, dict):
            return
        try:
            emit(AuditEvent.model_validate(raw), self._audit)
        except ValueError:
            _logger.warning("sync worker sent an unreadable audit event")

    # -- arcstore rows ---------------------------------------------------------

    async def _backend_now(self) -> Any:
        async with self._backend_lock:
            if self._backend is None:
                if self._opener is None:
                    raise RpcError("unavailable", "arcstore is not configured")
                self._backend = await self._opener()
            return self._backend

    def _owner(self, args: dict[str, Any]) -> str:
        owner = str(args.get("owner", ""))
        if not (is_knowledge_principal(owner) or owner in self._agents):
            raise RpcError("refused", "unknown store owner", reason="owner_not_served")
        return owner

    def _agent(self, args: dict[str, Any]) -> str:
        agent_did = str(args.get("agent_did", ""))
        if agent_did not in self._agents:
            raise RpcError("refused", "unknown agent", reason="agent_not_served")
        return agent_did

    async def _object_state(self, args: dict[str, Any]) -> ArcStoreObjectState:
        return ArcStoreObjectState(await self._backend_now(), actor_did=self._owner(args))

    async def _state_get(self, args: dict[str, Any]) -> Any:
        state = await (await self._object_state(args)).get_object_state(
            str(args["source_id"]), str(args["object_id"])
        )
        return None if state is None else state.model_dump(mode="json")

    async def _state_put(self, args: dict[str, Any]) -> Any:
        model = import_module("arcmemory.connected_data").ConnectedObjectState
        await (await self._object_state(args)).put_object_state(
            str(args["source_id"]), str(args["object_id"]), model.model_validate(args["state"])
        )
        return None

    async def _state_list(self, args: dict[str, Any]) -> Any:
        return await (await self._object_state(args)).list_object_ids(str(args["source_id"]))

    async def _state_clear(self, args: dict[str, Any]) -> Any:
        await (await self._object_state(args)).clear_source(str(args["source_id"]))
        return None

    async def _state_generation(self, args: dict[str, Any]) -> Any:
        return await (await self._object_state(args)).source_generation(str(args["connection_id"]))

    async def _approvals(self) -> Any:
        from arcstore.approvals import ApprovalStore

        store = ApprovalStore(await self._backend_now())
        await store.start()
        return store

    async def _approvals_list(self, args: dict[str, Any]) -> Any:
        agent_did = self._agent(args)
        status = args.get("status")
        rows = await (await self._approvals()).list(
            status=str(status) if isinstance(status, str) else None
        )
        return [row.model_dump(mode="json") for row in rows if row.agent_did == agent_did]

    async def _approvals_create(self, args: dict[str, Any]) -> Any:
        from arcstore.approvals import PendingApproval

        agent_did = self._agent(args)
        row = PendingApproval.model_validate(args.get("row"))
        if row.agent_did != agent_did or row.tool != _MAPPING_TOOL or row.status != "pending":
            raise RpcError("refused", "not a mapping proposal", reason="approval_not_allowed")
        created = await (await self._approvals()).create(row)
        return created.model_dump(mode="json")

    async def _registry(self) -> KnowledgeSubscriptions:
        return KnowledgeSubscriptions(await self._backend_now(), actor_did="did:arc:sync-worker")

    async def _subscription_get(self, args: dict[str, Any]) -> Any:
        row = await (await self._registry()).get(
            str(args["agent_did"]), str(args["connection_id"])
        )
        return None if row is None else _subscription_json(row)

    async def _subscriptions_for(self, args: dict[str, Any]) -> Any:
        rows = await (await self._registry()).for_connection(str(args["connection_id"]))
        return [_subscription_json(row) for row in rows]


#: The arcstore row operations a worker may ask for, by op name.
_ROW_OPS: dict[str, Callable[[HostService, dict[str, Any]], Awaitable[Any]]] = {
    "state.get": HostService._state_get,
    "state.put": HostService._state_put,
    "state.list": HostService._state_list,
    "state.clear": HostService._state_clear,
    "state.generation": HostService._state_generation,
    "approvals.list": HostService._approvals_list,
    "approvals.create": HostService._approvals_create,
    "subscriptions.get": HostService._subscription_get,
    "subscriptions.for_connection": HostService._subscriptions_for,
}


def _okf_sign(root: Path, message: bytes) -> bytes:
    try:
        sign_for = import_module("arcmemory.okf_seal").sign_for
    except ImportError as exc:
        raise PermissionError("no memory extra: nothing can be sealed") from exc
    signature: bytes = sign_for(root, message)
    return signature


def _subscription_json(row: KnowledgeSubscription) -> dict[str, str]:
    return {
        "agent_did": row.agent_did,
        "connection_id": row.connection_id,
        "source_id": row.source_id,
        "approval_id": row.approval_id,
        "profile": row.profile,
    }


# -- the worker's side -------------------------------------------------------


class HostClient:
    """The worker's authenticated line back to the main process."""

    def __init__(self, rpc: RpcClient, *, timeout: float = HOST_CALL_TIMEOUT) -> None:
        self._rpc = rpc
        self._timeout = timeout

    async def call(self, op: str, args: dict[str, Any]) -> Any:
        result, _ = await self._rpc.call(op, args, timeout=self._timeout)
        return result

    def sign(self, root: Path, message: bytes) -> bytes:
        """Blocking: a seal is signed from whatever thread is writing the index."""
        _, signature = self._rpc.call_blocking(
            "sign", {"root": str(root)}, message, timeout=self._timeout
        )
        return signature

    async def audit(self, event: AuditEvent) -> None:
        await self.call("audit", {"event": event.model_dump(mode="json")})


class HostObjectState:
    """Per-object versions for one store, kept in the main process's arcstore."""

    def __init__(self, host: HostClient, owner: str) -> None:
        self._host = host
        self._owner = owner

    async def get_object_state(self, source_id: str, object_id: str) -> Any | None:
        raw = await self._host.call(
            "state.get", {"owner": self._owner, "source_id": source_id, "object_id": object_id}
        )
        if raw is None:
            return None
        return import_module("arcmemory.connected_data").ConnectedObjectState.model_validate(raw)

    async def put_object_state(self, source_id: str, object_id: str, state: Any) -> None:
        await self._host.call(
            "state.put",
            {
                "owner": self._owner,
                "source_id": source_id,
                "object_id": object_id,
                "state": state.model_dump(mode="json"),
            },
        )

    async def list_object_ids(self, source_id: str) -> list[str]:
        ids = await self._host.call("state.list", {"owner": self._owner, "source_id": source_id})
        return [str(item) for item in ids]

    async def clear_source(self, source_id: str) -> None:
        await self._host.call("state.clear", {"owner": self._owner, "source_id": source_id})

    async def source_generation(self, connection_id: str) -> int:
        value = await self._host.call(
            "state.generation", {"owner": self._owner, "connection_id": connection_id}
        )
        return int(value)


class HostApprovals:
    """One agent's approval rows, read and proposed through the main process."""

    def __init__(self, host: HostClient, agent_did: str) -> None:
        self._host = host
        self._agent_did = agent_did

    async def start(self) -> None:
        return None

    async def list(self, *, status: str | None = None) -> list[Any]:
        from arcstore.approvals import PendingApproval

        rows = await self._host.call(
            "approvals.list", {"agent_did": self._agent_did, "status": status}
        )
        return [PendingApproval.model_validate(row) for row in rows]

    async def create(self, approval: Any) -> Any:
        from arcstore.approvals import PendingApproval

        row = await self._host.call(
            "approvals.create",
            {"agent_did": self._agent_did, "row": approval.model_dump(mode="json")},
        )
        return PendingApproval.model_validate(row)


class HostSubscriberAuthority:
    """A shared-store write, valid only while the writer's subscription stands.

    Asked before every object, so an agent whose grant was revoked mid-run stops
    writing the shared store at the next object.
    """

    def __init__(
        self, host: HostClient, agent_did: str, connection_id: str, approval_id: str
    ) -> None:
        self._host = host
        self._agent_did = agent_did
        self._connection_id = connection_id
        self._approval_id = approval_id

    async def authorized_homes(self) -> tuple[str, tuple[KnowledgeHome, ...]] | None:
        row = await self._host.call(
            "subscriptions.get",
            {"agent_did": self._agent_did, "connection_id": self._connection_id},
        )
        if not isinstance(row, dict) or row.get("approval_id") != self._approval_id:
            return None
        return self._approval_id, _DOCUMENT_ONLY


class HostMigrationAuthority:
    """A migrating agent's own approved document mapping, verified by the worker itself."""

    def __init__(self, host: HostClient, agent_did: str, approval_id: str) -> None:
        self._host = host
        self._agent_did = agent_did
        self._approval_id = approval_id

    async def authorized_homes(self) -> tuple[str, tuple[KnowledgeHome, ...]] | None:
        rows = await self._host.call(
            "approvals.list", {"agent_did": self._agent_did, "status": "approved"}
        )
        for row in rows:
            homes = str(row.get("arguments", {}).get("homes", ""))
            if row.get("id") == self._approval_id and homes == KnowledgeHome.DOCUMENT.value:
                return self._approval_id, _DOCUMENT_ONLY
        return None


class NoWrites:
    """The authority of a store that may only be purged: it authorizes no write."""

    async def authorized_homes(self) -> tuple[str, tuple[KnowledgeHome, ...]] | None:
        return None


class RemoteSealSigner:
    """A seal signer whose private key stays in the main process.

    Satisfies ``arcmemory.okf_seal.SealSigner``: the public half comes from the
    request, and every signature is made by the main process over the host
    channel.
    """

    def __init__(self, host: HostClient, root: Path, key: SealKey) -> None:
        self._host = host
        self._root = root
        self._key = key

    @property
    def did(self) -> str:
        return self._key.did

    @property
    def public_key(self) -> bytes:
        return bytes.fromhex(self._key.public_key)

    @property
    def algorithm(self) -> str:
        return self._key.algorithm

    def sign(self, message: bytes) -> bytes:
        """Signed by the main process; any refusal reads as "cannot sign" (withheld)."""
        try:
            return self._host.sign(self._root, message)
        except (RemoteError, RpcUnavailableError) as exc:
            raise PermissionError(f"seal not signed by the main process: {exc}") from exc


#: The audit events raised while one request runs; they return with its reply.
_REQUEST_EVENTS: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "sync_worker_request_events", default=None
)


class RequestAuditSink:
    """Collect a request's audit events for its reply; send any other event to the host.

    An event raised while a request runs belongs to the agent that asked for the
    write, so it returns with the reply and the main process emits it through
    that agent's own sink. One raised later (a debounced index drain) goes to
    the host's sink.
    """

    def __init__(self, host: HostClient) -> None:
        self._host = host
        self._pending: set[asyncio.Task[None]] = set()

    def write(self, event: AuditEvent) -> None:
        collected = _REQUEST_EVENTS.get()
        if collected is not None:
            collected.append(event.model_dump(mode="json"))
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop in this thread: the event is logged by emit's caller only
        task = loop.create_task(self._host.audit(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)


def collect_request_events() -> tuple[list[dict[str, Any]], contextvars.Token[Any]]:
    """Start collecting this request's audit events; reset with the returned token."""
    events: list[dict[str, Any]] = []
    return events, _REQUEST_EVENTS.set(events)


def stop_collecting(token: contextvars.Token[Any]) -> None:
    _REQUEST_EVENTS.reset(token)


__all__ = [
    "HostApprovals",
    "HostClient",
    "HostMigrationAuthority",
    "HostObjectState",
    "HostService",
    "HostSubscriberAuthority",
    "NoWrites",
    "RemoteSealSigner",
    "RequestAuditSink",
    "collect_request_events",
    "stop_collecting",
]
