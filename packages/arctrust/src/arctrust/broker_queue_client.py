"""Optional signed mTLS client for a broker-owned queue root and recovery fence."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arctrust.broker_queue_proofs import (
    BrokerQueueLease,
    BrokerQueueProofError,
    BrokerQueueRecoveryProof,
    verify_broker_queue_lease,
    verify_broker_queue_recovery,
)
from arctrust.canonical import canonical_json
from arctrust.monotonic import AnchorHead, AnchorUnavailableError
from arctrust.signer import ED25519, Signer

_OPERATION_DOMAIN = b"arc:machine-broker-operation:v1\0"
_EPOCH = r"^[1-9][0-9]{0,18}$"
_HEX_32 = r"^[0-9a-f]{64}$"


class QueueBrokerError(AnchorUnavailableError):
    """The broker cannot prove or advance a queue root under its live lease."""


def _is_intended_head(
    head: AnchorHead | None, expected: AnchorHead | None, digest: str, intent: str
) -> bool:
    return bool(
        head is not None
        and head.version == (expected.version + 1 if expected else 1)
        and head.scope == (expected.scope if expected else head.scope)
        and head.previous_digest == (expected.digest if expected else None)
        and head.digest == digest
        and head.intent == intent
    )


def _parse_proof(proof: str) -> dict[str, Any]:
    try:
        parsed = json.loads(proof)
        if (
            not isinstance(parsed, dict)
            or canonical_json(parsed).decode("utf-8") != proof
            or set(parsed) != {"facts", "signature"}
        ):
            raise ValueError("noncanonical queue proof")
        return parsed
    except (ValueError, TypeError, UnicodeError) as exc:
        raise QueueBrokerError("queue recovery proof invalid") from exc


class _BrokerResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    head: AnchorHead | None
    lease: dict[str, Any]
    recovery_proof: dict[str, Any] | None = None


class _SealedRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    ciphertext: str = Field(min_length=1, max_length=1_048_576)
    lease: dict[str, Any]


class _OpenedRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    plaintext: str = Field(min_length=1, max_length=700_000)
    lease: dict[str, Any]


class QueueBrokerAnchor:
    """One remote anchor and recovery authority bound to a signed machine lease.

    The supplied HTTPS client must already hold the deployment's mTLS capability.
    The broker validates the actual peer certificate, operation signature, live
    lease and queue root in its one authoritative CAS record. This client never
    treats an offline proof check as permission to mutate.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        tenant_id: str,
        journal_scope: str,
        machine_id: str,
        tls_fingerprint: str,
        signer: Signer,
        broker_public_key: bytes,
        lease_envelope: dict[str, Any],
        clock: Callable[[], int] | None = None,
    ) -> None:
        if client.base_url.scheme != "https" or client.base_url.userinfo:
            raise QueueBrokerError("queue broker requires credential-free HTTPS")
        if signer.algorithm != ED25519 or len(signer.public_key) != 32:
            raise QueueBrokerError("queue machine signer must be Ed25519")
        if len(broker_public_key) != 32 or re.fullmatch(_HEX_32, tls_fingerprint) is None:
            raise QueueBrokerError("queue broker trust material is invalid")
        self._client = client
        self._tenant_id = tenant_id
        self._scope = journal_scope
        self._machine_id = machine_id
        self._fingerprint = tls_fingerprint
        self._signer = signer
        self._broker_public_key = broker_public_key
        self._clock = clock or (lambda: int(time.time()))
        self._lock = threading.RLock()
        self._lease = self._verify_lease(lease_envelope)
        self._seen_head: AnchorHead | None = None
        self._validated_proofs: dict[str, str] = {}

    @property
    def scope(self) -> str:
        """Return the journal scope pinned by the signed machine lease."""
        return self._scope

    @property
    def owner_epoch(self) -> str:
        """Return the broker-issued epoch that agents must put in owner IDs."""
        return self._lease.active_owner_epoch

    @property
    def tenant_id(self) -> str:
        """Return the tenant pinned by the signed machine lease."""
        return self._tenant_id

    @property
    def fenced_through_epoch(self) -> str:
        """Return the highest prior epoch explicitly fenced by the broker."""
        return self._lease.fenced_through_epoch

    def _verify_lease(self, envelope: dict[str, Any]) -> BrokerQueueLease:
        try:
            lease = BrokerQueueLease.model_validate(envelope["facts"])
            verify_broker_queue_lease(
                envelope,
                issuer_public_key=self._broker_public_key,
                expected=lease,
                now=self._clock(),
            )
        except (BrokerQueueProofError, ValidationError, ValueError, TypeError, KeyError) as exc:
            raise QueueBrokerError("queue broker lease invalid") from exc
        if (
            lease.tenant_id != self._tenant_id
            or lease.journal_scope != self._scope
            or lease.machine_id != self._machine_id
            or lease.machine_public_key != self._signer.public_key.hex()
            or lease.tls_fingerprint != self._fingerprint
        ):
            raise QueueBrokerError("queue broker lease scope or lifetime invalid")
        return lease

    def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        response_limit: int = 16_384,
    ) -> tuple[int, bytes]:
        with self._client.stream(
            method,
            path,
            headers={**headers, "Accept-Encoding": "identity"},
            params=params,
            content=content,
            timeout=5,
            follow_redirects=False,
        ) as response:
            if 300 <= response.status_code < 400:
                raise QueueBrokerError("queue broker redirect refused")
            if response.status_code != 200:
                return response.status_code, b""
            if response.headers.get("content-encoding", "identity") != "identity":
                raise QueueBrokerError("queue broker compressed response refused")
            try:
                length = response.headers.get("content-length")
                if length is not None and int(length) > response_limit:
                    raise QueueBrokerError("queue broker response exceeds limit")
            except ValueError as exc:
                raise QueueBrokerError("queue broker response length invalid") from exc
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_raw(chunk_size=4096):
                size += len(chunk)
                if size > response_limit:
                    raise QueueBrokerError("queue broker response exceeds limit")
                chunks.append(chunk)
            return response.status_code, b"".join(chunks)

    def _accept_response(self, status: int, payload: bytes) -> _BrokerResponse:
        if status != 200 or len(payload) > 16_384:
            raise QueueBrokerError("queue broker response unavailable")
        try:
            body = _BrokerResponse.model_validate(json.loads(payload))
            if body.head is not None and body.head.scope != self._scope:
                raise QueueBrokerError("queue broker head scope mismatch")
            if self._seen_head is not None and (
                body.head is None
                or body.head.version < self._seen_head.version
                or (body.head.version == self._seen_head.version and body.head != self._seen_head)
            ):
                raise QueueBrokerError("queue broker head rollback detected")
            self._accept_lease(body.lease)
            self._seen_head = body.head
            return body
        except (ValidationError, ValueError, TypeError) as exc:
            raise QueueBrokerError("queue broker response invalid") from exc

    def _accept_lease(self, envelope: dict[str, Any]) -> None:
        lease = self._verify_lease(envelope)
        if (
            lease.lease_id != self._lease.lease_id
            or lease.active_owner_epoch != self.owner_epoch
            or lease.next_sequence < self._lease.next_sequence
            or int(lease.fenced_through_epoch) < int(self._lease.fenced_through_epoch)
        ):
            raise QueueBrokerError("queue broker owner changed")
        self._lease = lease

    def _record_request(self, action: Literal["seal", "open"], value: str) -> dict[str, Any]:
        with self._lock:
            path = f"/broker/record/{action}"
            field = "plaintext" if action == "seal" else "ciphertext"
            body = {
                "tenant_id": self._tenant_id,
                "journal_scope": self._scope,
                "purpose": "queue.record",
                field: value,
            }
            payload = canonical_json(body)
            sequence = self._lease.next_sequence
            operation = self._operation(
                f"record.{action}", "POST", path, hashlib.sha256(payload).hexdigest(), sequence
            )
            try:
                status, raw = self._request(
                    "POST",
                    path,
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Arc-Machine-Operation": operation,
                    },
                    response_limit=1_500_000,
                )
            except httpx.HTTPError as exc:
                raise QueueBrokerError("queue record outcome uncertain") from exc
            if status != 200:
                raise QueueBrokerError("queue record refused or uncertain")
            try:
                parsed = json.loads(raw)
                result = (
                    _SealedRecord.model_validate(parsed)
                    if action == "seal"
                    else _OpenedRecord.model_validate(parsed)
                )
            except (ValidationError, ValueError, TypeError) as exc:
                raise QueueBrokerError("queue record response invalid") from exc
            self._accept_lease(result.lease)
            if self._lease.next_sequence != sequence + 1:
                raise QueueBrokerError("queue record sequence did not advance")
            return result.model_dump(mode="json")

    def seal_record(self, payload: bytes) -> str:
        """Seal bounded bytes under the same lease sequence as queue root CAS."""
        if type(payload) is not bytes or len(payload) > 512 * 1024:
            raise QueueBrokerError("queue record plaintext exceeds limit")
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        return str(self._record_request("seal", encoded)["ciphertext"])

    def open_record(self, ciphertext: str) -> bytes:
        """Open one scoped Vault ciphertext through the live broker lease."""
        if type(ciphertext) is not str or not 0 < len(ciphertext) <= 1_048_576:
            raise QueueBrokerError("queue record ciphertext invalid")
        encoded = self._record_request("open", ciphertext)["plaintext"]
        if not isinstance(encoded, str) or "=" in encoded:
            raise QueueBrokerError("queue record plaintext encoding invalid")
        try:
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, TypeError) as exc:
            raise QueueBrokerError("queue record plaintext encoding invalid") from exc
        if (
            len(payload) > 512 * 1024
            or base64.urlsafe_b64encode(payload).decode().rstrip("=") != encoded
        ):
            raise QueueBrokerError("queue record plaintext encoding invalid")
        return payload

    def _operation(self, purpose: str, method: str, path: str, digest: str, sequence: int) -> str:
        facts = {
            "version": 1,
            "tenant_id": self._tenant_id,
            "machine_id": self._machine_id,
            "lease_id": self._lease.lease_id,
            "lease_epoch": int(self.owner_epoch),
            "tls_fingerprint": self._fingerprint,
            "purpose": purpose,
            "method": method,
            "path": path,
            "body_sha256": digest,
            "sequence": sequence,
            "nonce": secrets.token_urlsafe(32),
            "issued_at": self._clock(),
        }
        signature = self._signer.sign(_OPERATION_DOMAIN + canonical_json(facts))
        if len(signature) != 64:
            raise QueueBrokerError("queue machine signing failed")
        return (
            base64.urlsafe_b64encode(canonical_json({**facts, "signature": signature.hex()}))
            .decode()
            .rstrip("=")
        )

    def _read_head(self, prior_owner_epoch: str | None = None) -> _BrokerResponse:
        with self._lock:
            path = "/broker/queue/head"
            query = {"tenant_id": self._tenant_id, "journal_scope": self._scope}
            if prior_owner_epoch is not None:
                query["prior_owner_epoch"] = prior_owner_epoch
            digest = hashlib.sha256(canonical_json(query)).hexdigest()
            operation = self._operation("anchor.read", "GET", path, digest, 0)
            try:
                status, payload = self._request(
                    "GET",
                    path,
                    params=query,
                    headers={"X-Arc-Machine-Operation": operation},
                )
                return self._accept_response(status, payload)
            except httpx.HTTPError as exc:
                raise QueueBrokerError("queue broker head unavailable") from exc

    def latest(self) -> AnchorHead | None:
        """Read the broker's current queue head and refresh the signed lease."""
        return self._read_head().head

    def recovery_proof(self, prior_owner_epoch: str) -> str:
        """Obtain fresh issuer evidence for one already fenced prior owner."""
        if type(prior_owner_epoch) is not str or re.fullmatch(_EPOCH, prior_owner_epoch) is None:
            raise QueueBrokerError("queue recovery owner epoch invalid")
        response = self._read_head(prior_owner_epoch)
        if response.recovery_proof is None:
            raise QueueBrokerError("queue recovery proof unavailable")
        encoded = canonical_json(response.recovery_proof).decode()
        self._proof(encoded, prior_owner_epoch)
        return encoded

    def _proof(self, proof: str, prior_owner_epoch: str) -> BrokerQueueRecoveryProof:
        if type(proof) is not str or len(proof) > 8192:
            raise QueueBrokerError("queue recovery proof invalid")
        try:
            envelope = _parse_proof(proof)
            facts = BrokerQueueRecoveryProof.model_validate(envelope["facts"])
            verify_broker_queue_recovery(
                envelope,
                issuer_public_key=self._broker_public_key,
                expected=facts,
                now=self._clock(),
            )
        except (BrokerQueueProofError, ValidationError, ValueError, TypeError, KeyError) as exc:
            raise QueueBrokerError("queue recovery proof invalid") from exc
        if (
            facts.tenant_id != self._tenant_id
            or facts.journal_scope != self._scope
            or facts.prior_owner_epoch != prior_owner_epoch
            or facts.active_owner_epoch != self.owner_epoch
            or facts.lease_id != self._lease.lease_id
            or int(prior_owner_epoch) > int(self._lease.fenced_through_epoch)
        ):
            raise QueueBrokerError("queue recovery proof scope or lifetime invalid")
        return facts

    def validate(
        self,
        proof: str,
        *,
        journal_scope: str,
        tenant_id: str,
        owner_epoch: str,
        purpose: Literal["queue.recover"],
    ) -> None:
        """Preflight signed proof; mutation still checks live revocation in CAS."""
        if (
            journal_scope != self._scope
            or tenant_id != self._tenant_id
            or purpose != "queue.recover"
        ):
            raise QueueBrokerError("queue recovery scope mismatch")
        if re.fullmatch(_EPOCH, owner_epoch) is None:
            raise QueueBrokerError("queue recovery owner epoch invalid")
        with self._lock:
            self._proof(proof, owner_epoch)
            current = self._read_head(owner_epoch)
            if current.recovery_proof is None:
                raise QueueBrokerError("queue recovery proof unavailable")
            self._proof(canonical_json(current.recovery_proof).decode(), owner_epoch)
            self._validated_proofs[owner_epoch] = proof

    def compare_and_advance(
        self,
        expected: AnchorHead | None,
        digest: str,
        intent: str,
        proof: str | None = None,
        *,
        journal_scope: str | None = None,
        tenant_id: str | None = None,
        owner_epoch: str | None = None,
        purpose: Literal["queue.recover"] | None = None,
    ) -> AnchorHead:
        """Advance root through broker CAS, with atomic live fence for recovery."""
        if re.fullmatch(_HEX_32, digest) is None or len(intent) > 1_048_576:
            raise QueueBrokerError("queue head proposal invalid")
        recovery = proof is not None
        if recovery != (purpose == "queue.recover"):
            raise QueueBrokerError("queue recovery arguments incomplete")
        if recovery and (
            journal_scope != self._scope or tenant_id != self._tenant_id or owner_epoch is None
        ):
            raise QueueBrokerError("queue recovery scope mismatch")
        with self._lock:
            current_response = self._read_head(owner_epoch if recovery else None)
            current = current_response.head
            if current != expected:
                raise QueueBrokerError("queue broker CAS owner stale")
            if recovery:
                if proof is None or owner_epoch is None:
                    raise QueueBrokerError("queue recovery arguments incomplete")
                if self._validated_proofs.get(owner_epoch) != proof:
                    self._proof(proof, owner_epoch)
                if current_response.recovery_proof is None:
                    raise QueueBrokerError("queue recovery proof unavailable")
                fresh_proof = current_response.recovery_proof
                self._proof(canonical_json(fresh_proof).decode(), owner_epoch)
            path = "/broker/queue/recover-cas" if recovery else "/broker/queue/cas"
            body: dict[str, Any] = {
                "tenant_id": self._tenant_id,
                "journal_scope": self._scope,
                "expected_head": expected.model_dump(mode="json") if expected else None,
                "digest": digest,
                "intent": intent,
            }
            if recovery:
                body["prior_owner_epoch"] = owner_epoch
                body["recovery_proof"] = fresh_proof
            payload = canonical_json(body)
            operation = self._operation(
                "queue.recover" if recovery else "anchor.advance",
                "POST",
                path,
                hashlib.sha256(payload).hexdigest(),
                self._lease.next_sequence,
            )
            try:
                status, response_payload = self._request(
                    "POST",
                    path,
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Arc-Machine-Operation": operation,
                    },
                )
            except httpx.HTTPError:
                status, response_payload = 503, b""
            if status in {409, 503}:
                candidate = self.latest()
                if candidate is not None and _is_intended_head(
                    candidate, expected, digest, intent
                ):
                    return candidate
                raise QueueBrokerError("queue broker CAS outcome uncertain")
            if status != 200:
                raise QueueBrokerError("queue broker CAS refused")
            head = self._accept_response(status, response_payload).head
            if not _is_intended_head(head, expected, digest, intent):
                raise QueueBrokerError("queue broker head transition invalid")
            if head is None:
                raise QueueBrokerError("queue broker head missing")
            return head


class BrokerQueueByteCipher:
    """ByteCipher bound to the queue anchor's one mTLS lease and CAS sequence."""

    def __init__(self, anchor: QueueBrokerAnchor) -> None:
        self._anchor = anchor

    def seal(self, payload: bytes) -> str:
        """Seal bytes through the anchor's live broker session."""
        return self._anchor.seal_record(payload)

    def open(self, sealed: str) -> bytes:
        """Open bytes through the anchor's live broker session."""
        return self._anchor.open_record(sealed)
