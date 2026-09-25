"""Agent-owned accepted work with external manifest and fenced store projection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import arcrun
import arcstore
import arctrust
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from arcagent.core.run_contract import (
    CanonicalRunRequest,
    ChannelReply,
    ReplyLookup,
    ReplySender,
)


class RunIntentUnavailableError(RuntimeError):
    """Accepted work cannot be proven safe to start or recover."""


class RunAuthorizationRefusedError(RunIntentUnavailableError):
    """A signed claim is invalid or does not bind the requested operation."""


class RunOutcomeUnknownError(Exception):
    """An effect stopped with a bounded result that records its uncertainty."""

    def __init__(self, result: bytes) -> None:
        super().__init__("run outcome requires reconciliation")
        self.result = result


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_did: str
    tenant_id: str
    entries: dict[str, arcstore.StoredRunIntent] = Field(default_factory=dict)
    previous: dict[str, arcstore.StoredRunIntent] = Field(default_factory=dict)


def _canonical(manifest: _Manifest) -> str:
    return json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _scope(tenant_id: str, agent_did: str) -> str:
    identity = json.dumps([tenant_id, agent_did], ensure_ascii=True, separators=(",", ":"))
    return "runs/" + _digest("arc.run-intents.v1\0" + identity)


class VerifiedRunAuthorization(BaseModel):
    """Claims extracted only after authenticating a signed run trigger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1)
    agent_did: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    owner_epoch: int = Field(ge=1)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    deadline: datetime
    purpose: Literal["manual", "message", "schedule", "pulse", "workflow"]
    occurrence_id: str = Field(min_length=1)
    nonce: str = Field(min_length=1)
    action: Literal["execute", "reconcile"] = "execute"


RunAuthorizationVerifier = Callable[[bytes], VerifiedRunAuthorization]
RunEffect = Callable[[bytes], Awaitable[bytes]]
_logger = logging.getLogger(__name__)
_RESULT_ADAPTER = TypeAdapter(arcrun.RunResult)


class RunIntentLedger:
    """Owns reservation, authorization, execution, and crash reconciliation.

    One independently custodied anchor authenticates bounded enumeration for
    this agent. ArcStore holds encrypted blobs and versioned projections. A
    prior/final lag may be repaired; any larger divergence fails closed.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        agent_did: str,
        store: arcstore.AcceptedRunStore,
        anchor: arctrust.MonotonicAnchor,
        verify_authorization: RunAuthorizationVerifier,
        owner_epoch: int,
        max_intents: int = 1024,
        max_reserved_bytes: int = 64 * 1024 * 1024,
        anchor_timeout_seconds: float = 5.0,
    ) -> None:
        if (
            not tenant_id
            or not agent_did
            or owner_epoch < 1
            or max_intents < 1
            or max_reserved_bytes < 1
            or anchor_timeout_seconds <= 0
        ):
            raise ValueError("invalid run-intent scope or limits")
        if anchor.scope != _scope(tenant_id, agent_did):
            raise ValueError("run-intent anchor scope mismatch")
        self.tenant_id = tenant_id
        self.agent_did = agent_did
        self.owner_epoch = owner_epoch
        self._store = store
        self._anchor = anchor
        self._verify_authorization = verify_authorization
        self._max_intents = max_intents
        self._max_reserved_bytes = max_reserved_bytes
        self._anchor_timeout_seconds = anchor_timeout_seconds
        self._anchor_lock = asyncio.Lock()
        self._anchor_pending: asyncio.Task[arctrust.AnchorHead | None] | None = None

    async def _anchor_call(
        self, operation: Callable[[], arctrust.AnchorHead | None]
    ) -> arctrust.AnchorHead | None:
        async with self._anchor_lock:
            pending = self._anchor_pending
            if pending is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(pending), self._anchor_timeout_seconds)
                except TimeoutError as exc:
                    raise RunIntentUnavailableError(
                        "prior anchor call remains unresolved"
                    ) from exc
                except Exception as exc:
                    _logger.warning("prior anchor operation failed: %s", type(exc).__name__)
                self._anchor_pending = None
            task = asyncio.create_task(asyncio.to_thread(operation))
            self._anchor_pending = task
            try:
                return await asyncio.wait_for(asyncio.shield(task), self._anchor_timeout_seconds)
            except Exception as exc:
                raise RunIntentUnavailableError("run-intent anchor operation unavailable") from exc
            finally:
                if task.done():
                    self._anchor_pending = None

    def _verify(
        self,
        evidence: bytes,
        *,
        run_id: str,
        session_id: str,
        owner_epoch: int,
        deadline: datetime,
        request_digest: str,
        purpose: str,
        occurrence_id: str,
        action: Literal["execute", "reconcile"] = "execute",
    ) -> VerifiedRunAuthorization:
        try:
            claims = self._verify_authorization(evidence)
        except Exception as exc:
            raise RunAuthorizationRefusedError("signed run authorization refused") from exc
        if not isinstance(claims, VerifiedRunAuthorization) or (
            claims.tenant_id != self.tenant_id
            or claims.agent_did != self.agent_did
            or claims.run_id != run_id
            or claims.session_id != session_id
            or claims.owner_epoch != owner_epoch
            or claims.deadline != deadline
            or claims.request_digest != request_digest
            or claims.purpose != purpose
            or claims.occurrence_id != occurrence_id
            or claims.action != action
            or claims.deadline.tzinfo is None
            or claims.deadline.utcoffset() is None
        ):
            raise RunAuthorizationRefusedError("signed run authorization binding refused")
        return claims

    def authorization_action(self, evidence: bytes) -> Literal["execute", "reconcile"]:
        """Read a verified action claim; every operation rechecks all bound facts."""
        try:
            claims = self._verify_authorization(evidence)
        except Exception as exc:
            raise RunAuthorizationRefusedError("signed run authorization refused") from exc
        if not isinstance(claims, VerifiedRunAuthorization):
            raise RunAuthorizationRefusedError("signed run authorization type refused")
        return claims.action

    async def reconcile(
        self,
        *,
        run_id: str,
        session_id: str,
        request: bytes,
        signed_authorization: bytes,
        deadline: datetime,
        purpose: Literal["manual", "message", "schedule", "pulse", "workflow"],
        occurrence_id: str,
    ) -> arcstore.StoredRunIntent:
        """Authorize terminal read without extending an expired execution grant."""
        if (
            deadline.tzinfo is None
            or deadline.utcoffset() is None
            or deadline <= datetime.now(UTC)
        ):
            raise RunAuthorizationRefusedError("reconciliation deadline invalid")
        request_digest = hashlib.sha256(request).hexdigest()
        self._verify(
            signed_authorization,
            run_id=run_id,
            session_id=session_id,
            owner_epoch=self.owner_epoch,
            deadline=deadline,
            request_digest=request_digest,
            purpose=purpose,
            occurrence_id=occurrence_id,
            action="reconcile",
        )
        current = await self.get(run_id)
        if (
            current.status not in ("completed", "outcome_unknown")
            or current.session_id != session_id
            or current.purpose != purpose
            or current.occurrence_id != occurrence_id
            or current.request_digest != request_digest
        ):
            raise RunIntentUnavailableError("terminal run identity unavailable")
        return current

    @staticmethod
    def anchor_scope(tenant_id: str, agent_did: str) -> str:
        """Return the exact externally custodied scope for one agent ledger."""
        return _scope(tenant_id, agent_did)

    async def _read(self) -> tuple[arctrust.AnchorHead | None, _Manifest]:
        head = await self._anchor_call(self._anchor.latest)
        if head is None:
            return None, _Manifest(agent_did=self.agent_did, tenant_id=self.tenant_id)
        if head.scope != self._anchor.scope or _digest(head.intent) != head.digest:
            raise RunIntentUnavailableError("run-intent anchor integrity failed")
        try:
            manifest = _Manifest.model_validate_json(head.intent)
        except ValueError as exc:
            raise RunIntentUnavailableError("run-intent anchor payload invalid") from exc
        if manifest.agent_did != self.agent_did or manifest.tenant_id != self.tenant_id:
            raise RunIntentUnavailableError("run-intent anchor scope changed")
        if len(manifest.entries) > self._max_intents:
            raise RunIntentUnavailableError("run-intent manifest exceeds entry limit")
        return head, manifest

    async def _advance(
        self,
        expected: arctrust.AnchorHead | None,
        manifest: _Manifest,
        intent: arcstore.StoredRunIntent,
    ) -> None:
        entries = dict(manifest.entries)
        previous = dict(manifest.previous)
        prior = entries.get(intent.run_id)
        if prior is not None:
            previous[intent.run_id] = prior
        entries[intent.run_id] = intent
        if len(entries) > self._max_intents:
            raise RunIntentUnavailableError("run-intent capacity exhausted")
        if sum(item.reserved_bytes for item in entries.values()) > self._max_reserved_bytes:
            raise RunIntentUnavailableError("run-intent byte capacity exhausted")
        final = manifest.model_copy(update={"entries": entries, "previous": previous})
        payload = _canonical(final)
        if len(payload) > 1_048_576:
            raise RunIntentUnavailableError("run-intent anchor payload exceeds limit")
        digest = _digest(payload)
        try:
            written = await self._anchor_call(
                lambda: self._anchor.compare_and_advance(expected, digest, payload)
            )
            latest = await self._anchor_call(self._anchor.latest)
            if written is None:
                raise RunIntentUnavailableError("run-intent anchor write returned no head")
            if written.digest != digest or latest != written:
                raise RunIntentUnavailableError("run-intent anchor write unverified")
        except Exception as exc:
            raise RunIntentUnavailableError("run-intent anchor advancement unavailable") from exc

    async def _project(
        self, prior: arcstore.StoredRunIntent, final: arcstore.StoredRunIntent
    ) -> None:
        try:
            if not await self._store.compare_and_set(prior, final):
                raise RunIntentUnavailableError("run-intent CAS conflict")
        except Exception as exc:
            raise RunIntentUnavailableError("run-intent projection unavailable") from exc

    async def accept(
        self,
        *,
        run_id: str,
        session_id: str,
        owner_epoch: int,
        deadline: datetime,
        request: bytes,
        signed_authorization: bytes,
        max_result_bytes: int,
        purpose: Literal["manual", "message", "schedule", "pulse", "workflow"],
        occurrence_id: str,
    ) -> arcstore.StoredRunIntent:
        """Reserve before blobs, verify them, then anchor acceptance before CAS."""
        if (
            not run_id
            or not session_id
            or owner_epoch != self.owner_epoch
            or deadline.tzinfo is None
            or deadline.utcoffset() is None
            or deadline <= datetime.now(UTC)
            or max_result_bytes < 0
            or len(request) > self._store.max_blob_bytes
            or len(signed_authorization) > self._store.max_blob_bytes
            or max_result_bytes > self._store.max_blob_bytes
            or not signed_authorization
            or not occurrence_id
        ):
            raise ValueError("invalid run admission")
        request_digest = hashlib.sha256(request).hexdigest()
        claims = self._verify(
            signed_authorization,
            run_id=run_id,
            session_id=session_id,
            owner_epoch=owner_epoch,
            deadline=deadline,
            request_digest=request_digest,
            purpose=purpose,
            occurrence_id=occurrence_id,
        )
        head, manifest = await self._read()
        reserved_bytes = len(request) + len(signed_authorization) + max_result_bytes
        staging = manifest.entries.get(run_id)
        if staging is None:
            staging = arcstore.StoredRunIntent(
                run_id=run_id,
                tenant_id=self.tenant_id,
                agent_did=self.agent_did,
                session_id=session_id,
                purpose=purpose,
                occurrence_id=occurrence_id,
                authorization_nonce=claims.nonce,
                owner_epoch=owner_epoch,
                version=1,
                status="staging",
                deadline=deadline,
                authorization_digest=hashlib.sha256(signed_authorization).hexdigest(),
                request_digest=request_digest,
                reserved_bytes=reserved_bytes,
            )
            await self._advance(head, manifest, staging)
        elif (
            staging.session_id != session_id
            or staging.purpose != purpose
            or staging.occurrence_id != occurrence_id
            or staging.authorization_nonce != claims.nonce
            or staging.owner_epoch != owner_epoch
            or staging.deadline != deadline
            or staging.authorization_digest != hashlib.sha256(signed_authorization).hexdigest()
            or staging.request_digest != request_digest
            or staging.reserved_bytes != reserved_bytes
        ):
            raise RunIntentUnavailableError("run identity is reserved for different work")
        current = await self.get(run_id)
        if current.status != "staging":
            return current
        request_ref = await self._store.write_blob(
            self.tenant_id, self.agent_did, request, purpose="request"
        )
        auth_ref = await self._store.write_blob(
            self.tenant_id, self.agent_did, signed_authorization, purpose="authorization"
        )
        if (
            await self._store.read_blob(self.tenant_id, self.agent_did, request_ref) != request
            or await self._store.read_blob(self.tenant_id, self.agent_did, auth_ref)
            != signed_authorization
        ):
            raise RunIntentUnavailableError("run-intent blob readback failed")
        accepted = staging.model_copy(
            update={
                "status": "accepted",
                "version": 2,
                "request_ref": request_ref,
                "authorization_ref": auth_ref,
            }
        )
        head, manifest = await self._read()
        if manifest.entries.get(run_id) != current:
            raise RunIntentUnavailableError("run-intent reservation changed")
        await self._advance(head, manifest, accepted)
        await self._project(current, accepted)
        return accepted

    async def execute(
        self, intent: arcstore.StoredRunIntent, effect: RunEffect
    ) -> arcstore.StoredRunIntent:
        """Cross the persisted effect boundary once; uncertain errors stay uncertain."""
        current = await self.get(intent.run_id)
        if current != intent or current.status != "accepted":
            raise RunIntentUnavailableError("run is not freshly accepted")
        if current.deadline <= datetime.now(UTC) or current.authorization_ref is None:
            raise RunIntentUnavailableError("run authorization or deadline unavailable")
        evidence = await self._store.read_blob(
            self.tenant_id, self.agent_did, current.authorization_ref
        )
        if hashlib.sha256(evidence).hexdigest() != current.authorization_digest:
            raise RunIntentUnavailableError("run authorization no longer valid")
        claims = self._verify(
            evidence,
            run_id=current.run_id,
            session_id=current.session_id,
            owner_epoch=current.owner_epoch,
            deadline=current.deadline,
            request_digest=current.request_digest,
            purpose=current.purpose,
            occurrence_id=current.occurrence_id,
        )
        if claims.nonce != current.authorization_nonce:
            raise RunIntentUnavailableError("run authorization nonce changed")
        executing = current.model_copy(
            update={"status": "executing", "version": current.version + 1}
        )
        head, manifest = await self._read()
        await self._advance(head, manifest, executing)
        await self._project(current, executing)
        if executing.request_ref is None:
            raise RunIntentUnavailableError("accepted request is missing")
        if executing.authorization_ref is None:
            raise RunIntentUnavailableError("accepted authorization is missing")
        request = await self._store.read_blob(
            self.tenant_id, self.agent_did, executing.request_ref
        )
        try:
            result = await effect(request)
        except RunOutcomeUnknownError as exc:
            allowed_result = (
                executing.reserved_bytes
                - executing.request_ref.size
                - executing.authorization_ref.size
            )
            return await self.mark_unknown(
                executing,
                result=exc.result if len(exc.result) <= allowed_result else None,
            )
        except BaseException:
            await self.mark_unknown(executing)
            raise
        allowed_result = (
            executing.reserved_bytes
            - executing.request_ref.size
            - executing.authorization_ref.size
        )
        if len(result) > allowed_result:
            await self.mark_unknown(executing)
            raise RunIntentUnavailableError("run result exceeded reserved capacity")
        result_ref = await self._store.write_blob(
            self.tenant_id, self.agent_did, result, purpose="result"
        )
        completed = executing.model_copy(
            update={
                "status": "completed",
                "version": executing.version + 1,
                "result_ref": result_ref,
            }
        )
        head, manifest = await self._read()
        await self._advance(head, manifest, completed)
        await self._project(executing, completed)
        return completed

    async def mark_unknown(
        self, executing: arcstore.StoredRunIntent, *, result: bytes | None = None
    ) -> arcstore.StoredRunIntent:
        """Record uncertain effect outcome; no caller may retry execution."""
        head, manifest = await self._read()
        if manifest.entries.get(executing.run_id) != executing:
            raise RunIntentUnavailableError("executing run anchor changed")
        result_ref = None
        if result is not None:
            if executing.request_ref is None or executing.authorization_ref is None:
                raise RunIntentUnavailableError("run reservation is incomplete")
            available = (
                executing.reserved_bytes
                - executing.request_ref.size
                - executing.authorization_ref.size
            )
            if len(result) > available:
                raise RunIntentUnavailableError("unknown result exceeded reserved capacity")
            result_ref = await self._store.write_blob(
                self.tenant_id, self.agent_did, result, purpose="result"
            )
        unknown = executing.model_copy(
            update={
                "status": "outcome_unknown",
                "version": executing.version + 1,
                "result_ref": result_ref,
            }
        )
        await self._advance(head, manifest, unknown)
        await self._project(executing, unknown)
        return unknown

    async def get(self, run_id: str) -> arcstore.StoredRunIntent:
        """Read the externally authenticated run after reconciling one CAS lag."""
        _, manifest = await self._read()
        anchored = manifest.entries.get(run_id)
        if anchored is None:
            raise RunIntentUnavailableError("run is absent from authenticated manifest")
        row = await self._store.get(self.tenant_id, self.agent_did, run_id)
        if row == anchored:
            return anchored
        if row is None and anchored.version == 1 and anchored.status == "staging":
            await self._store.create(anchored)
            return anchored
        if row is None or row.version + 1 != anchored.version:
            raise RunIntentUnavailableError("run-intent projection diverged from anchor")
        if row != manifest.previous.get(run_id):
            raise RunIntentUnavailableError("run-intent projection is not the anchored prior")
        await self._project(row, anchored)
        return anchored

    async def result_bytes(self, intent: arcstore.StoredRunIntent) -> bytes:
        """Read a completed result only after matching the authenticated manifest."""
        current = await self.get(intent.run_id)
        if (
            current != intent
            or current.status not in ("completed", "outcome_unknown")
            or current.result_ref is None
        ):
            raise RunIntentUnavailableError("terminal run result unavailable")
        return await self._store.read_blob(self.tenant_id, self.agent_did, current.result_ref)

    async def _reply_dispatch(self, intent: arcstore.StoredRunIntent) -> ChannelReply | None:
        if (
            intent.status != "completed"
            or intent.purpose != "message"
            or intent.request_ref is None
            or intent.authorization_ref is None
            or intent.result_ref is None
        ):
            raise RunIntentUnavailableError("reply requires a completed message run")
        body = await self._store.read_blob(self.tenant_id, self.agent_did, intent.request_ref)
        request = CanonicalRunRequest.model_validate_json(body)
        if (
            request.canonical_bytes() != body
            or request.run_id != intent.run_id
            or request.session_key != intent.session_id
            or request.occurrence_id != intent.occurrence_id
            or request.digest() != intent.request_digest
        ):
            raise RunIntentUnavailableError("reply request differs from accepted run")
        evidence = await self._store.read_blob(
            self.tenant_id, self.agent_did, intent.authorization_ref
        )
        self._verify(
            evidence,
            run_id=intent.run_id,
            session_id=intent.session_id,
            owner_epoch=intent.owner_epoch,
            deadline=intent.deadline,
            request_digest=intent.request_digest,
            purpose=intent.purpose,
            occurrence_id=intent.occurrence_id,
        )
        if request.reply_target is None or not request.reply_target.startswith("channel://"):
            return None
        payload = await self.result_bytes(intent)
        result = _RESULT_ADAPTER.validate_json(payload)
        if result.outcome_unknown is not None or not result.content:
            return None
        digest = hashlib.sha256(result.content.encode()).hexdigest()
        identity = json.dumps(
            [self.tenant_id, self.agent_did, intent.run_id, request.reply_target, digest],
            separators=(",", ":"),
        )
        message_id = "reply_" + _digest("arc.channel-reply.v1\0" + identity)
        return ChannelReply(
            message_id=message_id,
            target=request.reply_target,
            text=result.content,
            digest=digest,
        )

    async def _reply_transition(
        self,
        current: arcstore.StoredRunIntent,
        state: Literal["pending", "sending", "sent", "outcome_unknown"],
        message_id: str,
        digest: str,
    ) -> arcstore.StoredRunIntent:
        head, manifest = await self._read()
        if manifest.entries.get(current.run_id) != current:
            raise RunIntentUnavailableError("reply state changed under another owner")
        if current.reply_state != "none" and (
            current.reply_message_id != message_id or current.reply_digest != digest
        ):
            raise RunIntentUnavailableError("reply identity changed")
        updated = current.model_copy(
            update={
                "version": current.version + 1,
                "reply_state": state,
                "reply_message_id": message_id,
                "reply_digest": digest,
            }
        )
        await self._advance(head, manifest, updated)
        await self._project(current, updated)
        return updated

    async def deliver_reply(
        self, run_id: str, *, send: ReplySender, lookup: ReplyLookup
    ) -> Literal["sent", "outcome_unknown", "not_applicable"]:
        """Send a completed result once; uncertain sends require durable reconciliation."""
        current = await self.get(run_id)
        reply = await self._reply_dispatch(current)
        if reply is None:
            return "not_applicable"
        if current.reply_state == "sent":
            return "sent"
        if current.reply_state in ("sending", "outcome_unknown"):
            if await self._reply_visible(reply, lookup):
                await self._reply_transition(current, "sent", reply.message_id, reply.digest)
                return "sent"
            if current.reply_state == "sending":
                await self._reply_transition(
                    current, "outcome_unknown", reply.message_id, reply.digest
                )
            return "outcome_unknown"
        if current.reply_state == "none":
            current = await self._reply_transition(
                current, "pending", reply.message_id, reply.digest
            )
        current = await self._reply_transition(current, "sending", reply.message_id, reply.digest)
        try:
            await send(reply)
        except Exception:
            _logger.exception("channel reply publish outcome uncertain: %s", run_id)
        if await self._reply_visible(reply, lookup):
            await self._reply_transition(current, "sent", reply.message_id, reply.digest)
            return "sent"
        await self._reply_transition(current, "outcome_unknown", reply.message_id, reply.digest)
        return "outcome_unknown"

    @staticmethod
    async def _reply_visible(reply: ChannelReply, lookup: ReplyLookup) -> bool:
        try:
            return await lookup(reply)
        except Exception:
            _logger.exception("channel reply reconciliation unavailable: %s", reply.message_id)
            return False

    async def recover(self) -> tuple[arcstore.StoredRunIntent, ...]:
        """Reconcile all anchored intents; executing ones become outcome unknown."""
        _, manifest = await self._read()
        recovered: list[arcstore.StoredRunIntent] = []
        for run_id in manifest.entries:
            intent = await self.get(run_id)
            if intent.status == "executing":
                intent = await self.mark_unknown(intent)
            if intent.reply_state == "sending":
                if intent.reply_message_id is None or intent.reply_digest is None:
                    raise RunIntentUnavailableError("sending reply has no anchored identity")
                intent = await self._reply_transition(
                    intent, "outcome_unknown", intent.reply_message_id, intent.reply_digest
                )
            recovered.append(intent)
        return tuple(recovered)
