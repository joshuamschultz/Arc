"""Zero-config operator-signed custodian for schedule and pulse revisions.

Personal and enterprise deployments have no external control service, yet every
scheduled run must still be an approved artifact. This authority keeps one
:class:`~arctrust.file_journal_anchor.FileJournalAnchor` per
``(tenant, agent, purpose, artifact)``: an append-only, hash-chained, operator
signed journal whose head *is* the current approved revision.

* ``register_revision`` checks a fresh actor proof (the agent's enrolled key, or
  the operator's), enforces the caller's ``expected_revision`` against the head
  (compare-and-swap), signs a :class:`SignedControlRevision` and appends it.
* ``verify_current`` is the dispatch gate: the presented approval must be the
  exact, operator-signed, unrevoked head, bound to the definition about to run,
  and each occurrence is admitted once per process.
* ``revoke`` appends a revoked head; a revoked artifact never fires again.

What it defends and what it cannot is the anchor's: an attacker who edits the
journal, forges a line, or copies an approval into ``schedules.json`` is refused;
one who restores a consistent older copy of every file to a process that has not
seen the newer head is not. Every event is therefore audited with
``custody="local_file"``, and federal deployments do not use this class.

The authority never holds key material — only an operator :class:`Signer`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.control import (
    CONTROL_PURPOSES,
    ControlArtifactRefusedError,
    ControlArtifactUnavailableError,
    ControlPurpose,
    SignedControlRevision,
    control_revision_message,
    parse_control_actor_proof,
    sign_control_actor_proof,
)
from arctrust.file_journal_anchor import LOCAL_ANCHOR_CUSTODY, FileJournalAnchor
from arctrust.identity import did_matches_pubkey
from arctrust.monotonic import AnchorHead, AnchorUnavailableError
from arctrust.signer import Signer, verify_signature

_MAX_ID = 512
_TRIGGER_DOMAIN = b"arc.local-run-trigger.v1\n"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _BoundedSet:
    """Insertion-ordered set that forgets its oldest member past ``limit``."""

    def __init__(self, limit: int) -> None:
        self._items: OrderedDict[str, None] = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def add_new(self, item: str) -> bool:
        """Record ``item``; False when it was already present."""
        with self._lock:
            if item in self._items:
                return False
            self._items[item] = None
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
            return True


class LocalControlArtifactAuthority:
    """Operator-signed local journal implementing ``ControlArtifactAuthority``."""

    def __init__(
        self,
        journal_dir: Path,
        *,
        signer: Signer,
        operator_did: str,
        clock: Callable[[], datetime] = _utc_now,
        audit_sink: AuditSink | None = None,
        proof_window: timedelta = timedelta(minutes=5),
        max_remembered: int = 10_000,
    ) -> None:
        if not operator_did:
            raise ValueError("operator DID is required")
        self._directory = Path(journal_dir)
        self._signer = signer
        self._operator_did = operator_did
        self._operator_key = (signer.algorithm, bytes(signer.public_key))
        self._clock = clock
        self._audit_sink = audit_sink
        self._proof_window = proof_window
        self._actors: dict[str, tuple[str, bytes]] = {operator_did: self._operator_key}
        self._anchors: dict[str, FileJournalAnchor] = {}
        self._lock = threading.Lock()
        self._proof_nonces = _BoundedSet(max_remembered)
        self._occurrences = _BoundedSet(max_remembered)

    @property
    def operator_did(self) -> str:
        """DID of the operator key that signs every revision."""
        return self._operator_did

    # ------------------------------------------------------------ actors

    def enroll_actor(self, did: str, public_key: bytes, algorithm: str) -> None:
        """Pin the key ``did`` must prove with; a different key is refused.

        Called by the agent runtime with the identity it loaded from its own key
        file. The DID must be derived from that key, and a pinned DID can never
        be re-pinned to another key in this process.
        """
        key = (algorithm, bytes(public_key))
        with self._lock:
            pinned = self._actors.get(did)
            if pinned is not None:
                if pinned != key:
                    raise ControlArtifactRefusedError("control actor key is already pinned")
                return
            if not did_matches_pubkey(did, key[1]):
                raise ControlArtifactRefusedError("control actor DID does not match its key")
            self._actors[did] = key

    def operator_proof(
        self, purpose: ControlPurpose, artifact_id: str, definition: bytes
    ) -> bytes:
        """A fresh operator actor proof, for operator-role surfaces (the dashboard)."""
        return sign_control_actor_proof(
            self._signer,
            actor_did=self._operator_did,
            purpose=purpose,
            artifact_id=artifact_id,
            definition=definition,
            issued_at=self._clock(),
        )

    # --------------------------------------------------------- contract

    async def register_revision(
        self,
        *,
        tenant_id: str,
        agent_did: str,
        purpose: ControlPurpose,
        artifact_id: str,
        canonical_definition: bytes,
        expected_revision: int | None,
        actor_proof: bytes,
    ) -> SignedControlRevision:
        """Approve the next revision iff the actor proves it and the CAS still holds."""
        target = (tenant_id, agent_did, purpose, artifact_id)
        try:
            self._check_target(*target)
            actor_did = self._verify_actor(
                actor_proof, agent_did, purpose, artifact_id, canonical_definition
            )
            approval = await asyncio.to_thread(
                self._append,
                target,
                canonical_definition,
                expected_revision,
                actor_did,
            )
        except (ControlArtifactRefusedError, ControlArtifactUnavailableError) as exc:
            self._audit("register", agent_did, purpose, artifact_id, "deny", reason=str(exc))
            raise
        self._audit("register", agent_did, purpose, artifact_id, "allow", approval.revision)
        return approval

    async def verify_current(
        self,
        *,
        tenant_id: str,
        agent_did: str,
        purpose: ControlPurpose,
        artifact_id: str,
        canonical_definition: bytes,
        approval: SignedControlRevision,
        occurrence_id: str,
    ) -> None:
        """Admit one occurrence of the exact current, unrevoked, signed revision."""
        target = (tenant_id, agent_did, purpose, artifact_id)
        try:
            self._check_target(*target)
            if not occurrence_id or len(occurrence_id) > _MAX_ID:
                raise ControlArtifactRefusedError("control occurrence is invalid")
            self._check_binding(approval, target, _sha256(canonical_definition))
            current = await asyncio.to_thread(self._current, target)
            if current is None or current != approval:
                raise ControlArtifactRefusedError("control approval is not the current revision")
            if current.revoked:
                raise ControlArtifactRefusedError("control artifact is revoked")
            if not self._occurrences.add_new(
                _sha256(json.dumps([*target, occurrence_id]).encode())
            ):
                raise ControlArtifactRefusedError("control occurrence was already admitted")
        except (ControlArtifactRefusedError, ControlArtifactUnavailableError) as exc:
            self._audit("verify", agent_did, purpose, artifact_id, "deny", reason=str(exc))
            raise
        self._audit("verify", agent_did, purpose, artifact_id, "allow", approval.revision)

    async def revoke(
        self, *, tenant_id: str, agent_did: str, purpose: ControlPurpose, artifact_id: str
    ) -> SignedControlRevision:
        """Append a revoked head; the artifact never verifies or advances again."""
        target = (tenant_id, agent_did, purpose, artifact_id)
        self._check_target(*target)
        revoked = await asyncio.to_thread(self._revoke, target)
        self._audit("revoke", agent_did, purpose, artifact_id, "allow", revoked.revision)
        return revoked

    # ----------------------------------------------------------- checks

    @staticmethod
    def _check_target(tenant_id: str, agent_did: str, purpose: str, artifact_id: str) -> None:
        for value in (tenant_id, agent_did, artifact_id):
            if type(value) is not str or not value or len(value) > _MAX_ID:
                raise ControlArtifactRefusedError("control target is invalid")
        if purpose not in CONTROL_PURPOSES:
            raise ControlArtifactRefusedError("control purpose is invalid")

    def _verify_actor(
        self,
        actor_proof: bytes,
        agent_did: str,
        purpose: str,
        artifact_id: str,
        definition: bytes,
    ) -> str:
        proof = parse_control_actor_proof(actor_proof)
        if (
            proof.purpose != purpose
            or proof.artifact_id != artifact_id
            or proof.definition_digest != _sha256(definition)
        ):
            raise ControlArtifactRefusedError("control actor proof names another definition")
        if proof.actor_did not in (agent_did, self._operator_did):
            raise ControlArtifactRefusedError("control actor may not act for this agent")
        with self._lock:
            pinned = self._actors.get(proof.actor_did)
        if pinned is None:
            raise ControlArtifactRefusedError("control actor is not enrolled")
        algorithm, public_key = pinned
        if proof.algorithm != algorithm or not hmac.compare_digest(
            bytes.fromhex(proof.public_key), public_key
        ):
            raise ControlArtifactRefusedError("control actor proof key is not the actor's")
        if not verify_signature(
            algorithm, proof.signed_message(), bytes.fromhex(proof.signature), public_key
        ):
            raise ControlArtifactRefusedError("control actor proof signature is invalid")
        if abs(self._clock() - proof.issued_at) > self._proof_window:
            raise ControlArtifactRefusedError("control actor proof is not fresh")
        if not self._proof_nonces.add_new(proof.nonce):
            raise ControlArtifactRefusedError("control actor proof was already used")
        return proof.actor_did

    def _check_binding(
        self,
        approval: SignedControlRevision,
        target: tuple[str, str, str, str],
        digest: str,
    ) -> None:
        if (
            (approval.tenant_id, approval.agent_did, approval.purpose, approval.artifact_id)
            != target
            or approval.definition_digest != digest
            or approval.revoked
        ):
            raise ControlArtifactRefusedError("control approval does not bind its definition")
        if not self._signed_by_operator(approval):
            raise ControlArtifactRefusedError("control approval signature is invalid")

    def _signed_by_operator(self, revision: SignedControlRevision) -> bool:
        algorithm, public_key = self._operator_key
        return verify_signature(
            algorithm,
            control_revision_message(revision),
            bytes.fromhex(revision.signature),
            public_key,
        )

    # ---------------------------------------------------------- journal

    def _anchor(self, target: tuple[str, str, str, str]) -> FileJournalAnchor:
        scope = "control/" + _sha256(json.dumps(list(target)).encode("utf-8"))
        with self._lock:
            anchor = self._anchors.get(scope)
            if anchor is None:
                anchor = FileJournalAnchor(
                    self._directory, scope=scope, signer=self._signer, actor_did=self._operator_did
                )
                self._anchors[scope] = anchor
            return anchor

    def _head(
        self, target: tuple[str, str, str, str]
    ) -> tuple[AnchorHead | None, SignedControlRevision | None]:
        anchor = self._anchor(target)
        try:
            head = anchor.latest()
        except AnchorUnavailableError as exc:
            raise ControlArtifactUnavailableError("control journal cannot be verified") from exc
        if head is None:
            return None, None
        try:
            revision = SignedControlRevision.model_validate_json(head.intent)
        except ValueError as exc:
            raise ControlArtifactUnavailableError("control journal head is malformed") from exc
        if (
            (revision.tenant_id, revision.agent_did, revision.purpose, revision.artifact_id)
            != target
            or revision.revision != head.version
            or revision.definition_digest != head.digest
            or not self._signed_by_operator(revision)
        ):
            raise ControlArtifactUnavailableError("control journal head does not bind its scope")
        return head, revision

    def _current(self, target: tuple[str, str, str, str]) -> SignedControlRevision | None:
        return self._head(target)[1]

    def _append(
        self,
        target: tuple[str, str, str, str],
        definition: bytes,
        expected_revision: int | None,
        actor_did: str,
    ) -> SignedControlRevision:
        head, current = self._head(target)
        if (None if head is None else head.version) != expected_revision:
            raise ControlArtifactRefusedError("control revision is stale")
        if current is not None and current.revoked:
            raise ControlArtifactRefusedError("control artifact is revoked")
        return self._advance(target, head, _sha256(definition), actor_did, revoked=False)

    def _revoke(self, target: tuple[str, str, str, str]) -> SignedControlRevision:
        head, current = self._head(target)
        if head is None or current is None:
            raise ControlArtifactRefusedError("control artifact has no revision to revoke")
        if current.revoked:
            return current
        return self._advance(target, head, head.digest, self._operator_did, revoked=True)

    def _advance(
        self,
        target: tuple[str, str, str, str],
        head: AnchorHead | None,
        digest: str,
        actor_did: str,
        *,
        revoked: bool,
    ) -> SignedControlRevision:
        tenant_id, agent_did, purpose, artifact_id = target
        unsigned = SignedControlRevision(
            tenant_id=tenant_id,
            agent_did=agent_did,
            purpose=cast(ControlPurpose, purpose),
            artifact_id=artifact_id,
            revision=1 if head is None else head.version + 1,
            definition_digest=digest,
            actor_did=actor_did,
            issued_at=self._clock(),
            revoked=revoked,
            signature="00",
        )
        try:
            signature = self._signer.sign(control_revision_message(unsigned)).hex()
            revision = unsigned.model_copy(update={"signature": signature})
            self._anchor(target).compare_and_advance(head, digest, revision.model_dump_json())
        except AnchorUnavailableError as exc:
            raise ControlArtifactUnavailableError("control journal cannot advance") from exc
        except Exception as exc:  # reason: any signer failure must fail closed
            raise ControlArtifactUnavailableError("control signing is unavailable") from exc
        return revision

    # ------------------------------------------------------------ audit

    def _audit(
        self,
        operation: str,
        agent_did: str,
        purpose: str,
        artifact_id: str,
        outcome: str,
        revision: int | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        if self._audit_sink is None:
            return
        extra: dict[str, Any] = {"custody": LOCAL_ANCHOR_CUSTODY, "agent_did": agent_did}
        if revision is not None:
            extra["revision"] = revision
        if reason is not None:
            extra["reason"] = reason
        emit(
            AuditEvent(
                actor_did=self._operator_did,
                action=f"control_artifact.{operation}",
                target=f"{purpose}:{artifact_id}",
                outcome=outcome,
                extra=extra,
            ),
            self._audit_sink,
        )


class _DigestibleRequest(Protocol):
    def digest(self) -> str: ...


class LocalRunTriggerIssuer:
    """Operator-signed authorization for one exact run request and its evidence.

    The local counterpart of an external trigger service: it binds the canonical
    request digest and the evidence (the verified occurrence and approval) under
    the operator signature, with a bounded deadline.
    """

    def __init__(
        self,
        signer: Signer,
        *,
        clock: Callable[[], datetime] = _utc_now,
        ttl: timedelta = timedelta(hours=1),
    ) -> None:
        self._signer = signer
        self._clock = clock
        self._ttl = ttl

    async def __call__(
        self, request: _DigestibleRequest, evidence: bytes
    ) -> tuple[bytes, datetime]:
        """Sign ``{request_digest, evidence_digest, deadline}``; return it and the deadline."""
        deadline = self._clock() + self._ttl
        unsigned = {
            "request_digest": request.digest(),
            "evidence_digest": _sha256(evidence),
            "deadline": deadline.isoformat(),
            "algorithm": self._signer.algorithm,
            "public_key": bytes(self._signer.public_key).hex(),
        }
        message = _TRIGGER_DOMAIN + json.dumps(
            unsigned, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        try:
            signature = self._signer.sign(message).hex()
        except Exception as exc:  # reason: any signer failure must fail closed
            raise ControlArtifactUnavailableError("run trigger signing is unavailable") from exc
        body = {**unsigned, "signature": signature}
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"), deadline


__all__ = ["LocalControlArtifactAuthority", "LocalRunTriggerIssuer"]
