"""Encrypted hosted claim state in one externally anchored compare-and-swap head."""

from __future__ import annotations

import hashlib
import json
import time
from threading import RLock
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from arctrust.audit import AuditEvent, DurableAuditSink
from arctrust.deployment_grant import DeploymentChallenge, DeploymentGrant
from arctrust.identity import did_from_public_key
from arctrust.machine_rekey import (
    MachineRekeyError,
    MachineRekeyGrant,
    MachineRekeyIntent,
    verify_rekey_grant,
)
from arctrust.monotonic import AnchorHead, MonotonicAnchor


class HostedJournalError(RuntimeError):
    """Hosted claim state cannot be authenticated or atomically continued."""


class _Cipher(Protocol):
    def seal(self, payload: bytes) -> str: ...

    def open(self, sealed: str) -> bytes: ...


class _State(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    intent: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    challenge: DeploymentChallenge
    epoch: int = Field(ge=1)
    grant: dict[str, Any] | None = None


class HostedClaimJournal:
    """Expose a claim head while sealing the entire state in its remote CAS intent."""

    def __init__(self, anchor: MonotonicAnchor, cipher: _Cipher) -> None:
        self._anchor = anchor
        self._cipher = cipher
        self._lock = RLock()

    @property
    def scope(self) -> str:
        return self._anchor.scope

    def _read(self) -> tuple[AnchorHead | None, _State | None]:
        head = self._anchor.latest()
        if head is None:
            return None, None
        try:
            if (
                head.scope != self._anchor.scope
                or hashlib.sha256(head.intent.encode("ascii")).hexdigest() != head.digest
            ):
                raise ValueError("hosted ciphertext head mismatch")
            state = _State.model_validate_json(self._cipher.open(head.intent))
            if state.intent.startswith("claimed:") and state.grant is not None:
                raise ValueError("completed claim retained grant")
            return head, state
        except (UnicodeError, ValueError, TypeError, KeyError) as exc:
            raise HostedJournalError("hosted claim journal refused") from exc

    def latest(self) -> AnchorHead | None:
        """Read the externally anchored decrypted state revision."""
        head, state = self._read()
        if head is None or state is None:
            return None
        return AnchorHead(
            scope=head.scope,
            version=head.version,
            digest=state.digest,
            previous_digest=state.previous_digest,
            intent=state.intent,
        )

    def current(self) -> tuple[DeploymentChallenge, int, dict[str, Any] | None] | None:
        """Load challenge and sealed grant for restart, without any private key."""
        _, state = self._read()
        if state is None:
            return None
        return state.challenge, state.epoch, state.grant

    def initialize(self, challenge: DeploymentChallenge, *, epoch: int = 1) -> AnchorHead:
        """Persist a fresh challenge before publishing it to the cloud."""
        digest = hashlib.sha256(challenge.canonical_bytes()).hexdigest()
        with self._lock:
            if self._anchor.latest() is not None:
                raise HostedJournalError("hosted journal already initialized")
            state = _State(intent="challenge", digest=digest, challenge=challenge, epoch=epoch)
            return self._advance(None, state)

    def install_grant(self, envelope: dict[str, Any], digest: str, claim_id: str) -> AnchorHead:
        """Persist the verified grant before any browser claim may begin."""
        with self._lock:
            previous, state = self._read()
            if previous is None or state is None or state.intent != "challenge":
                raise HostedJournalError("hosted grant cannot replace current state")
            next_state = _State.model_validate(
                {
                    **state.model_dump(mode="json"),
                    **{
                        "intent": f"granted:{claim_id}",
                        "digest": digest,
                        "previous_digest": state.digest,
                        "grant": envelope,
                    },
                }
            )
            return self._advance(previous, next_state)

    def rotate_challenge(
        self, challenge: DeploymentChallenge, *, epoch: int, now: int | None = None
    ) -> AnchorHead:
        """Fence an expired unclaimed challenge or grant with a new epoch."""
        with self._lock:
            previous, state = self._read()
            if previous is None or state is None:
                raise HostedJournalError("hosted journal is not initialized")
            if state.intent != "challenge" and not state.intent.startswith("granted:"):
                raise HostedJournalError("hosted claim cannot rotate")
            moment = int(time.time()) if now is None else now
            expiry = state.challenge.expires_at
            if state.grant is not None:
                expiry = DeploymentGrant.model_validate(state.grant["facts"]).claim_expires_at
            if moment < expiry:
                raise HostedJournalError("hosted claim is not expired")
            expected_epoch = state.epoch + (1 if state.intent.startswith("granted:") else 0)
            if epoch != expected_epoch:
                raise HostedJournalError("hosted epoch transition refused")
            if challenge.expires_at <= state.challenge.expires_at:
                raise HostedJournalError("hosted challenge did not advance")
            immutable = (
                "order_id",
                "server_id",
                "domain",
                "release_id",
                "arc_image_digest",
                "machine_public_key",
            )
            if any(
                getattr(challenge, name) != getattr(state.challenge, name) for name in immutable
            ):
                raise HostedJournalError("hosted machine identity changed")
            next_state = _State(
                intent="challenge",
                digest=hashlib.sha256(challenge.canonical_bytes()).hexdigest(),
                previous_digest=state.digest,
                challenge=challenge,
                epoch=epoch,
            )
            return self._advance(previous, next_state)

    def rekey_challenge(
        self,
        envelope: dict[str, Any],
        *,
        issuer_public_key: bytes,
        tenant_id: str,
        audit_sink: DurableAuditSink,
        now: int | None = None,
    ) -> AnchorHead:
        """Replace a lost ephemeral boot key only on dual-signed cloud authority."""
        with self._lock:
            previous, state = self._read()
            if (
                previous is None
                or state is None
                or not (state.intent == "challenge" or state.intent.startswith("granted:"))
            ):
                raise HostedJournalError("hosted rekey state is unavailable")
            actor_did = did_from_public_key(
                bytes.fromhex(state.challenge.machine_public_key),
                org=tenant_id,
                agent_type="machine",
            )
            try:
                grant = MachineRekeyGrant.model_validate(envelope["facts"])
                intent = MachineRekeyIntent.model_validate(grant.intent["facts"])
                verify_rekey_grant(
                    envelope,
                    issuer_public_key=issuer_public_key,
                    expected=grant,
                    now=now,
                )
                old_challenge = state.challenge
                next_challenge = intent.challenge
                old_head = AnchorHead(
                    scope=self.scope,
                    version=previous.version,
                    digest=state.digest,
                    previous_digest=state.previous_digest,
                    intent=state.intent,
                )
                immutable = (
                    "order_id",
                    "server_id",
                    "domain",
                    "release_id",
                    "arc_image_digest",
                )
                if (
                    intent.previous_head_scope != old_head.scope
                    or intent.previous_head_version != old_head.version
                    or intent.previous_head_digest != old_head.digest
                    or intent.previous_challenge_digest
                    != hashlib.sha256(old_challenge.canonical_bytes()).hexdigest()
                    or intent.current_epoch != state.epoch
                    or intent.next_epoch != state.epoch + int(state.grant is not None)
                    or next_challenge.machine_public_key == old_challenge.machine_public_key
                    or next_challenge.nonce == old_challenge.nonce
                    or next_challenge.issued_at <= old_challenge.issued_at
                    or any(
                        getattr(next_challenge, name) != getattr(old_challenge, name)
                        for name in immutable
                    )
                ):
                    raise ValueError("machine rekey binding changed")
                if state.grant is not None:
                    old_grant = DeploymentGrant.model_validate(state.grant["facts"])
                    if (
                        grant.customer_id != old_grant.customer_id
                        or grant.subscription_id != old_grant.subscription_id
                    ):
                        raise ValueError("machine rekey customer changed")
                audit_sink.write_durable(
                    AuditEvent(
                        actor_did=actor_did,
                        action="hosted.claim.rekey",
                        target=self.scope,
                        outcome="attempt",
                    )
                )
                next_state = _State(
                    intent="challenge",
                    digest=hashlib.sha256(next_challenge.canonical_bytes()).hexdigest(),
                    previous_digest=state.digest,
                    challenge=next_challenge,
                    epoch=intent.next_epoch,
                )
                return self._advance(previous, next_state)
            except (KeyError, TypeError, ValueError, MachineRekeyError, HostedJournalError) as exc:
                audit_sink.write_durable(
                    AuditEvent(
                        actor_did=actor_did,
                        action="hosted.claim.rekey",
                        target=self.scope,
                        outcome="deny",
                    )
                )
                raise HostedJournalError("hosted machine rekey refused") from exc

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        """Reserve or complete the current grant through the same remote CAS."""
        with self._lock:
            previous, state = self._read()
            if previous is None or state is None or self.latest() != expected:
                raise HostedJournalError("hosted journal revision changed")
            claim_id = state.intent.partition(":")[2]
            if not (
                (
                    state.intent == f"granted:{claim_id}"
                    and intent == f"pending:{claim_id}"
                    and digest == state.digest
                )
                or (state.intent == f"pending:{claim_id}" and intent == f"claimed:{claim_id}")
            ):
                raise HostedJournalError("hosted journal transition refused")
            next_state = _State.model_validate(
                {
                    **state.model_dump(mode="json"),
                    **{
                        "intent": intent,
                        "digest": digest,
                        "previous_digest": state.digest,
                        "grant": None if intent.startswith("claimed:") else state.grant,
                    },
                }
            )
            return self._advance(previous, next_state)

    def _advance(self, expected: AnchorHead | None, state: _State) -> AnchorHead:
        sealed = self._cipher.seal(
            json.dumps(
                state.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        )
        digest = hashlib.sha256(sealed.encode("ascii")).hexdigest()
        returned = self._anchor.compare_and_advance(expected, digest, sealed)
        if (
            returned.scope != self.scope
            or returned.version != (1 if expected is None else expected.version + 1)
            or returned.digest != digest
            or returned.previous_digest != (expected.digest if expected else None)
            or returned.intent != sealed
        ):
            raise HostedJournalError("hosted journal CAS returned conflicting evidence")
        return AnchorHead(
            scope=self.scope,
            version=returned.version,
            digest=state.digest,
            previous_digest=state.previous_digest,
            intent=state.intent,
        )
