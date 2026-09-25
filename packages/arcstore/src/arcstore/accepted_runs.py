"""Encrypted request/result blobs and versioned accepted agent-run rows."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, Protocol

import arctrust
from pydantic import BaseModel, ConfigDict, Field, field_validator

RunIntentStatus = Literal[
    "staging", "accepted", "executing", "completed", "failed", "outcome_unknown"
]
_BLOB_COLLECTION = "accepted_run_blobs"
_INTENT_COLLECTION = "accepted_run_intents"


class RunBlobRef(BaseModel):
    """Content identity of an encrypted body scoped to one tenant and agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    purpose: Literal["request", "authorization", "result"]


class StoredRunIntent(BaseModel):
    """The persistent projection of one agent-owned accepted run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    agent_did: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    purpose: Literal["manual", "message", "schedule", "pulse", "workflow"]
    occurrence_id: str = Field(min_length=1)
    authorization_nonce: str = Field(min_length=1)
    owner_epoch: int = Field(ge=1)
    version: int = Field(ge=1)
    status: RunIntentStatus
    deadline: datetime
    authorization_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_ref: RunBlobRef | None = None
    request_ref: RunBlobRef | None = None
    result_ref: RunBlobRef | None = None
    reserved_bytes: int = Field(ge=0)
    reply_state: Literal["none", "pending", "sending", "sent", "outcome_unknown"] = "none"
    reply_message_id: str | None = None
    reply_digest: str | None = None

    @field_validator("deadline")
    @classmethod
    def aware_deadline(cls, value: datetime) -> datetime:
        """Reject ambiguous local timestamps at the persistence boundary."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("run deadline must have a timezone")
        return value


class AcceptedRunBackend(Protocol):
    """Only the adjacent mutable-plane calls needed for accepted work."""

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def mutable_write(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> None: ...

    async def mutable_create_batch(
        self,
        collection: str,
        entries: list[tuple[str, dict[str, Any]]],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> list[dict[str, Any]]: ...

    async def update_if(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        where: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
        absent_where: dict[str, Any] | None = None,
    ) -> bool: ...


RunStoreAuthorizer = Callable[[str, str, str], bool]


def _key(tenant_id: str, agent_did: str, purpose: str, value: str) -> str:
    identity = json.dumps(
        [tenant_id, agent_did, purpose, value], ensure_ascii=True, separators=(",", ":")
    )
    return hashlib.sha256(b"arc.accepted-run.v1\0" + identity.encode()).hexdigest()


class AcceptedRunStore:
    """ArcStore implementation of encrypted blobs and fenced intent CAS.

    The caller owns the cipher capability and all run admission/anchor policy.
    ArcStore never interprets an agent's authorization evidence or starts work.
    """

    def __init__(
        self,
        backend: AcceptedRunBackend,
        cipher: arctrust.RecordCipher,
        *,
        authorize: RunStoreAuthorizer,
        audit_sink: arctrust.DurableAuditSink,
        max_blob_bytes: int = 1_048_576,
    ) -> None:
        if max_blob_bytes < 1:
            raise ValueError("max_blob_bytes must be positive")
        self._backend = backend
        self._cipher = cipher
        self._authorize = authorize
        self._audit_sink = audit_sink
        self._max_blob_bytes = max_blob_bytes

    @property
    def max_blob_bytes(self) -> int:
        """Return the largest body this store can seal for a reserved run."""
        return self._max_blob_bytes

    def _access(self, action: str, tenant_id: str, agent_did: str) -> None:
        """Authorize and audit before any protected storage operation."""
        try:
            allowed = self._authorize(action, tenant_id, agent_did)
        except Exception:
            allowed = False
        self._audit_sink.write_durable(
            arctrust.AuditEvent(
                actor_did=agent_did,
                action=f"accepted_run.{action}",
                target=_key(tenant_id, agent_did, action, "scope"),
                outcome="allow" if allowed else "deny",
            )
        )
        if not allowed:
            raise PermissionError("accepted-run storage operation refused")

    async def write_blob(
        self,
        tenant_id: str,
        agent_did: str,
        body: bytes,
        *,
        purpose: Literal["request", "authorization", "result"],
    ) -> RunBlobRef:
        """Seal a bounded body and verify the persisted bytes before returning."""
        if not tenant_id or not agent_did or len(body) > self._max_blob_bytes:
            raise ValueError("invalid accepted-run blob scope or size")
        self._access("write_blob", tenant_id, agent_did)
        ref = RunBlobRef(sha256=hashlib.sha256(body).hexdigest(), size=len(body), purpose=purpose)
        key = _key(tenant_id, agent_did, purpose, ref.sha256)
        sealed = self._cipher.seal(
            {
                "extra": {
                    "tenant_id": tenant_id,
                    "agent_did": agent_did,
                    "ref": ref.model_dump(),
                    "body": base64.b64encode(body).decode("ascii"),
                }
            }
        )
        await self._backend.mutable_create_batch(
            _BLOB_COLLECTION,
            [
                (
                    key,
                    {
                        "tenant_id": tenant_id,
                        "agent_did": agent_did,
                        "ref": ref.model_dump(),
                        "sealed": sealed,
                    },
                )
            ],
            actor_did=agent_did,
        )
        if await self.read_blob(tenant_id, agent_did, ref) != body:
            raise ValueError("accepted-run blob readback mismatch")
        return ref

    async def read_blob(self, tenant_id: str, agent_did: str, ref: RunBlobRef) -> bytes:
        """Open only a body whose scope, size, and content hash match the reference."""
        self._access("read_blob", tenant_id, agent_did)
        key = _key(tenant_id, agent_did, ref.purpose, ref.sha256)
        row = await self._backend.mutable_read(_BLOB_COLLECTION, key)
        if (
            row is None
            or row.get("tenant_id") != tenant_id
            or row.get("agent_did") != agent_did
            or row.get("ref") != ref.model_dump()
        ):
            raise ValueError("accepted-run blob unavailable for scope")
        sealed = row["sealed"]
        if not isinstance(sealed, dict) or set(sealed.get("extra", {})) != {"arc.audit.sealed"}:
            raise ValueError("accepted-run blob is not sealed")
        opened = self._cipher.unseal(sealed)
        content = opened.get("extra", {})
        if (
            content.get("tenant_id") != tenant_id
            or content.get("agent_did") != agent_did
            or content.get("ref") != ref.model_dump()
        ):
            raise ValueError("accepted-run blob sealed scope mismatch")
        raw = content.get("body")
        if not isinstance(raw, str):
            raise ValueError("accepted-run blob payload missing")
        body = base64.b64decode(raw, validate=True)
        if len(body) != ref.size or hashlib.sha256(body).hexdigest() != ref.sha256:
            raise ValueError("accepted-run blob integrity failed")
        return body

    async def create(self, intent: StoredRunIntent) -> StoredRunIntent:
        """Persist the initial staging projection; duplicate ids are refused."""
        if intent.status != "staging" or intent.version != 1:
            raise ValueError("initial accepted-run projection must be staging version 1")
        self._access("create_intent", intent.tenant_id, intent.agent_did)
        key = _key(intent.tenant_id, intent.agent_did, "intent", intent.run_id)
        rows = await self._backend.mutable_create_batch(
            _INTENT_COLLECTION,
            [(key, intent.model_dump(mode="json"))],
            actor_did=intent.agent_did,
        )
        if (
            StoredRunIntent.model_validate(
                {name: value for name, value in rows[0].items() if name != "updated_at"}
            )
            != intent
        ):
            raise ValueError("accepted-run identity already exists")
        return intent

    async def get(self, tenant_id: str, agent_did: str, run_id: str) -> StoredRunIntent | None:
        """Read one agent-scoped projection by its exact identity."""
        self._access("read_intent", tenant_id, agent_did)
        row = await self._backend.mutable_read(
            _INTENT_COLLECTION, _key(tenant_id, agent_did, "intent", run_id)
        )
        if row is None:
            return None
        intent = StoredRunIntent.model_validate(
            {key: value for key, value in row.items() if key != "updated_at"}
        )
        if (intent.tenant_id, intent.agent_did, intent.run_id) != (tenant_id, agent_did, run_id):
            raise ValueError("accepted-run projection scope mismatch")
        return intent

    async def compare_and_set(self, prior: StoredRunIntent, final: StoredRunIntent) -> bool:
        """Advance one version under the original tenant, agent and owner epoch."""
        if (
            final.version != prior.version + 1
            or final.tenant_id != prior.tenant_id
            or final.agent_did != prior.agent_did
            or final.run_id != prior.run_id
            or final.owner_epoch != prior.owner_epoch
        ):
            raise ValueError("invalid accepted-run transition identity or version")
        self._access("update_intent", prior.tenant_id, prior.agent_did)
        return await self._backend.update_if(
            _INTENT_COLLECTION,
            _key(prior.tenant_id, prior.agent_did, "intent", prior.run_id),
            final.model_dump(mode="json"),
            where={
                "tenant_id": prior.tenant_id,
                "agent_did": prior.agent_did,
                "owner_epoch": prior.owner_epoch,
                "version": prior.version,
                "status": prior.status,
            },
            actor_did=prior.agent_did,
        )
