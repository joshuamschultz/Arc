"""What a write names (the store), and the typed errors that cross the process boundary."""

from __future__ import annotations

from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict

from arcagent.connected_data import (
    MappingDeniedError,
    MappingPendingError,
    ObjectNotIngestibleError,
    SyncError,
)
from arcagent.modules.connected_data.sync_worker.rpc import RemoteError, RpcError


class SealKey(BaseModel):
    """The public half of the key a store's seal is signed with. Never the private half."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    did: str
    public_key: str
    algorithm: str


#: Who authorizes a write:
#: ``owner`` an agent's own store (its approval rows decide);
#: ``subscriber`` a shared store, under the writing agent's live subscription;
#: ``migration`` a shared store, under the agent's own approved document mapping;
#: ``orphan`` a shared store nobody reads any more: it may only be purged.
Authority = Literal["owner", "subscriber", "migration", "orphan"]


class StoreSpec(BaseModel):
    """One document store, as the main process names it to the worker.

    The worker never trusts ``root`` on its own: it derives the store's root
    itself (from the agent's own config, or from the connection id) and refuses
    a request whose ``root`` disagrees.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["own", "shared"]
    agent_did: str
    root: str
    authority: Authority
    #: ``own`` only: the agent's ``arcagent.toml``, whose identity and workspace
    #: the worker reads itself.
    config_path: str = ""
    #: ``shared`` only.
    connection_id: str = ""
    approval_id: str = ""
    seal: SealKey | None = None
    #: The memory module's ``(embed_backend, embed_model, embed_base_url)``.
    embed: tuple[str, str, str] | None = None


class SyncWorkerUnavailableError(SyncError):
    """The sync worker is down or restarting: retry later, nothing was lost.

    A sync run that meets this ends at its last committed page and is deferred
    (like a rate limit), never charged as a failure of the source.
    """

    code = "sync_worker_unavailable"


class SyncWorkerRefusedError(MappingDeniedError):
    """The worker refused the write: the store or its authorization did not check."""


class SyncWorkerWriteError(RuntimeError):
    """The write failed inside the worker for a reason that is not about authorization."""


def error_to_wire(exc: BaseException) -> RpcError:
    """A worker-side failure, typed for the caller."""
    if isinstance(exc, RpcError):
        return exc
    if isinstance(exc, ObjectNotIngestibleError):
        return RpcError("object_not_ingestible", exc.public_message, reason=exc.reason)
    if isinstance(exc, MappingPendingError):
        return RpcError("mapping_pending", exc.public_message)
    if isinstance(exc, MappingDeniedError):
        return RpcError("mapping_denied", exc.public_message)
    if isinstance(exc, SyncError):
        return RpcError(
            "sync_error", exc.public_message, code=exc.code, retry_after=exc.retry_after
        )
    return RpcError("write_failed", type(exc).__name__)


def raise_from_wire(error: RemoteError) -> NoReturn:
    """Re-raise a worker's typed error as the exception the coordinator acts on."""
    wire: dict[str, Any] = error.error
    kind = error.kind
    message = str(wire.get("message", ""))
    if kind == "object_not_ingestible":
        raise ObjectNotIngestibleError(str(wire.get("reason", "")), message) from error
    if kind == "mapping_pending":
        raise MappingPendingError() from error
    if kind == "mapping_denied":
        raise MappingDeniedError(message or "mapping denied") from error
    if kind == "refused":
        raise SyncWorkerRefusedError(f"sync worker refused: {wire.get('reason', '')}") from error
    if kind == "sync_error":
        retry = wire.get("retry_after")
        raise SyncError(
            message or "sync failed",
            code=str(wire.get("code", "")) or None,
            retry_after=float(retry) if isinstance(retry, (int, float)) else None,
        ) from error
    if kind == "unavailable":
        raise SyncWorkerUnavailableError(message or "sync worker is unavailable") from error
    raise SyncWorkerWriteError(message or kind) from error


__all__ = [
    "Authority",
    "SealKey",
    "StoreSpec",
    "SyncWorkerRefusedError",
    "SyncWorkerUnavailableError",
    "SyncWorkerWriteError",
    "error_to_wire",
    "raise_from_wire",
]
