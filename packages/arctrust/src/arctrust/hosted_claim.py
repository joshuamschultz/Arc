"""One-time hosted first operator claim over an independent monotonic head."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import ValidationError

from arctrust.audit import AuditEvent, DurableAuditSink
from arctrust.deployment_grant import (
    DeploymentChallenge,
    DeploymentGrant,
    DeploymentGrantError,
    sign_challenge,
    verify_deployment_grant,
)
from arctrust.hosted_journal import HostedClaimJournal
from arctrust.identity import did_from_public_key, parse_did
from arctrust.monotonic import MonotonicAnchor
from arctrust.users import OPERATOR, User


class HostedClaimError(RuntimeError):
    """The hosted first operator cannot be safely claimed."""


class _Users(Protocol):
    def is_empty(self) -> bool: ...

    def get(self, email: str) -> User | None: ...

    def claim_first_operator(
        self, email: str, password: str, *, org: str, claim_digest: str
    ) -> User: ...


class _Authority(Protocol):
    def user_store(self, actor_proof: object) -> _Users: ...


class HostedFirstClaim:
    """Reserve before account effects and reconcile an uncertain account write.

    The deployment owns the claim anchor and the actor verifier behind the
    account authority. The cloud issuer never gets direct account-key custody.
    """

    def __init__(
        self,
        *,
        challenge: DeploymentChallenge,
        expected_epoch: int,
        issuer_public_key: bytes,
        anchor: MonotonicAnchor,
        authority: _Authority,
        actor_proof: object,
        tenant_id: str,
        audit_sink: DurableAuditSink,
        machine_signer: Callable[[bytes], bytes],
    ) -> None:
        if expected_epoch < 1 or len(issuer_public_key) != 32:
            raise HostedClaimError("hosted claim trust inputs are invalid")
        self._challenge = challenge
        self._expected_epoch = expected_epoch
        self._issuer_public_key = issuer_public_key
        self._anchor = anchor
        self._authority = authority
        self._actor_proof = actor_proof
        self._tenant_id = tenant_id
        self._audit_sink = audit_sink
        self._machine_signer = machine_signer
        self._grant_envelope: dict[str, Any] | None = None
        if isinstance(anchor, HostedClaimJournal):
            current = anchor.current()
            if current is None or current[:2] != (challenge, expected_epoch):
                raise HostedClaimError("hosted journal boot facts mismatch")
            self._grant_envelope = current[2]

    def signed_challenge(self, *, now: int | None = None) -> dict[str, Any]:
        """Expose current machine proof without exposing its ephemeral private key."""
        self._refresh_expired(now=now)
        head = self._anchor.latest()
        digest = hashlib.sha256(self._challenge.canonical_bytes()).hexdigest()
        if head is None or head.intent != "challenge" or head.digest != digest:
            raise HostedClaimError("machine challenge is not current")
        return sign_challenge(self._challenge, self._machine_signer)

    def _refresh_expired(self, *, now: int | None) -> None:
        journal = self._anchor
        if not isinstance(journal, HostedClaimJournal):
            return
        current = journal.current()
        if current is None:
            raise HostedClaimError("hosted challenge state is unavailable")
        if current[:2] != (self._challenge, self._expected_epoch):
            head = journal.latest()
            if head is None or head.intent != "challenge" or current[2] is not None:
                raise HostedClaimError("hosted claim changed during refresh")
            self._challenge, self._expected_epoch, self._grant_envelope = current
            self._audit("refresh", "allow")
        moment = int(time.time()) if now is None else now
        head = journal.latest()
        if head is None or head.intent.startswith(("pending:", "claimed:")):
            return
        expiry = self._challenge.expires_at
        if self._grant_envelope is not None:
            try:
                expiry = DeploymentGrant.model_validate(
                    self._grant_envelope["facts"]
                ).claim_expires_at
            except (KeyError, ValidationError, TypeError):
                raise HostedClaimError("hosted grant state is invalid") from None
        if moment < expiry:
            return
        if not self._authority.user_store(self._actor_proof).is_empty():
            raise HostedClaimError("initial account already exists")
        next_challenge = self._challenge.model_copy(update={
            "nonce": secrets.token_urlsafe(32), "issued_at": moment,
            "expires_at": moment + 300,
        })
        self._audit("refresh", "attempt")
        next_epoch = self._expected_epoch + (1 if self._grant_envelope is not None else 0)
        journal.rotate_challenge(next_challenge, epoch=next_epoch, now=moment)
        self._challenge = next_challenge
        self._expected_epoch = next_epoch
        self._grant_envelope = None
        self._audit("refresh", "allow")

    def arm(self) -> None:
        """Bind the boot challenge to a durable external head before exposure."""
        head = self._anchor.latest()
        digest = hashlib.sha256(self._challenge.canonical_bytes()).hexdigest()
        if head is None:
            self._audit("challenge", "attempt")
            self._anchor.compare_and_advance(None, digest, "challenge")
            self._audit("challenge", "allow")
            return
        if head.intent == "challenge" and head.digest == digest:
            self._audit("challenge", "allow")
            return
        if head.intent.startswith(("granted:", "pending:", "claimed:")):
            return
        raise HostedClaimError("hosted claim head conflicts with boot challenge")

    def install_grant(self, envelope: dict[str, Any], *, now: int | None = None) -> None:
        """Accept issuer evidence over the machine channel, never from the browser."""
        grant = self._verified_grant(envelope, now=now)
        head = self._anchor.latest()
        challenge_digest = hashlib.sha256(self._challenge.canonical_bytes()).hexdigest()
        grant_digest = hashlib.sha256(grant.canonical_bytes()).hexdigest()
        if head is None or not (
            (head.intent == "challenge" and head.digest == challenge_digest)
            or (
                head.intent == f"granted:{grant.customer_claim_id}"
                and head.digest == grant_digest
            )
            or (
                head.intent == f"pending:{grant.customer_claim_id}"
                and head.digest == grant_digest
            )
        ):
            raise HostedClaimError("hosted grant cannot be installed at this revision")
        if self._grant_envelope is not None and self._grant_envelope != envelope:
            raise HostedClaimError("hosted grant was already installed")
        self._audit("grant", "attempt")
        if isinstance(self._anchor, HostedClaimJournal) and head.intent == "challenge":
            self._anchor.install_grant(envelope, grant_digest, grant.customer_claim_id)
        self._audit("grant", "allow")
        self._grant_envelope = envelope

    def status(self) -> str:
        """Return customer setup state independently from service readiness."""
        head = self._anchor.latest()
        if head is None:
            return "unavailable"
        if head.intent.startswith("claimed:"):
            return "setup_complete"
        if self._grant_envelope is not None:
            return "awaiting_setup"
        return "awaiting_grant"

    def claim(
        self,
        customer_secret: str,
        password: str,
        *,
        now: int | None = None,
    ) -> User:
        """Create the first operator once; a pending retry reconciles the same grant."""
        envelope = self._grant_envelope
        if envelope is None:
            raise HostedClaimError("hosted grant is unavailable")
        grant = self._verified_grant(envelope, now=now, claim_lifetime=True)
        self._verify_customer_secret(grant, customer_secret)
        store = self._authority.user_store(self._actor_proof)
        grant_digest = hashlib.sha256(grant.canonical_bytes()).hexdigest()
        challenge_digest = hashlib.sha256(self._challenge.canonical_bytes()).hexdigest()
        head = self._anchor.latest()
        if head is None:
            raise HostedClaimError("hosted claim head is unavailable")
        pending = f"pending:{grant.customer_claim_id}"
        if (
            (head.intent == "challenge" and head.digest == challenge_digest)
            or (
                head.intent == f"granted:{grant.customer_claim_id}"
                and head.digest == grant_digest
            )
        ):
            if not store.is_empty():
                raise HostedClaimError("initial account already exists")
            self._audit("reserve", "attempt")
            self._anchor.compare_and_advance(head, grant_digest, pending)
            self._audit("reserve", "allow")
        elif head.intent != pending or head.digest != grant_digest:
            raise HostedClaimError("hosted claim was already used or replaced")
        else:
            self._audit("reserve", "allow")
        user = store.get(grant.customer_email)
        if user is None:
            if not store.is_empty():
                raise HostedClaimError("another account owns the first claim")
            user = store.claim_first_operator(
                grant.customer_email, password,
                org=self._tenant_id, claim_digest=grant_digest,
            )
        elif (
            user.roles != (OPERATOR,)
            or user.initial_claim_digest != grant_digest
            or parse_did(user.did)["org"] != self._tenant_id
        ):
            raise HostedClaimError("pending operator identity conflicts")
        current = self._anchor.latest()
        if current is None or current.intent != pending or current.digest != grant_digest:
            raise HostedClaimError("hosted claim changed during account creation")
        self._audit("complete", "attempt")
        self._anchor.compare_and_advance(
            current,
            hashlib.sha256((grant_digest + user.did).encode()).hexdigest(),
            f"claimed:{grant.customer_claim_id}",
        )
        self._audit("complete", "allow")
        return user

    def _verified_grant(
        self, envelope: dict[str, Any], *, now: int | None,
        claim_lifetime: bool = False,
    ) -> DeploymentGrant:
        try:
            grant = DeploymentGrant.model_validate(envelope["facts"])
            verify_deployment_grant(
                envelope,
                issuer_public_key=self._issuer_public_key,
                expected=grant,
                now=now,
                claim_lifetime=claim_lifetime,
            )
            fields = DeploymentChallenge.model_fields
            if any(getattr(grant, field) != getattr(self._challenge, field) for field in fields):
                raise ValueError("machine challenge mismatch")
            if grant.epoch != self._expected_epoch:
                raise ValueError("hosted claim epoch mismatch")
            return grant
        except (KeyError, TypeError, ValueError, ValidationError, DeploymentGrantError) as exc:
            self._audit("verify", "deny")
            raise HostedClaimError("hosted first claim refused") from exc

    def _verify_customer_secret(self, grant: DeploymentGrant, secret: str) -> None:
        if len(secret) < 32 or not hmac.compare_digest(
            hashlib.sha256(secret.encode()).hexdigest(), grant.customer_claim_secret_sha256
        ):
            self._audit("customer", "deny")
            raise HostedClaimError("hosted first claim refused")

    def _audit(self, action: str, outcome: str) -> None:
        self._audit_sink.write_durable(
            AuditEvent(
                actor_did=did_from_public_key(
                    bytes.fromhex(self._challenge.machine_public_key),
                    org=self._tenant_id,
                    agent_type="machine",
                ),
                action=f"hosted.claim.{action}",
                target=self._anchor.scope,
                outcome=outcome,
            )
        )
