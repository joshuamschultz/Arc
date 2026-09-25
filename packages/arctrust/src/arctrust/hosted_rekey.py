"""Restart-only rekey coordination for an unclaimed hosted account."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from typing import Any

from arctrust.audit import DurableAuditSink
from arctrust.deployment_grant import DeploymentChallenge
from arctrust.hosted_claim import HostedFirstClaim
from arctrust.hosted_journal import HostedClaimJournal, HostedJournalError
from arctrust.machine_rekey import (
    MachineRekeyError,
    MachineRekeyGrant,
    MachineRekeyIntent,
    sign_rekey_intent,
    verify_rekey_grant,
)
from arctrust.users import User


class HostedRekeyError(RuntimeError):
    """A pending hosted machine key cannot acquire the current claim head."""


class HostedRekeyCoordinator:
    """Hold one ephemeral key only until cloud-endorsed journal rotation."""

    def __init__(
        self,
        *,
        journal: HostedClaimJournal,
        machine_public_key: bytes,
        machine_signer: Callable[[bytes], bytes],
        issuer_public_key: bytes,
        tenant_id: str,
        audit_sink: DurableAuditSink,
        first_claim_factory: Callable[[DeploymentChallenge, int], HostedFirstClaim],
    ) -> None:
        if len(machine_public_key) != 32 or len(issuer_public_key) != 32:
            raise HostedRekeyError("hosted rekey keys are invalid")
        self._journal = journal
        self._machine_public_key = machine_public_key
        self._machine_signer = machine_signer
        self._issuer_public_key = issuer_public_key
        self._tenant_id = tenant_id
        self._audit_sink = audit_sink
        self._first_claim_factory = first_claim_factory
        self._claim_service: HostedFirstClaim | None = None
        self._claim_facts: tuple[DeploymentChallenge, int] | None = None
        self._pending: dict[str, Any] | None = None

    def _claim(self) -> HostedFirstClaim:
        current = self._journal.current()
        if current is None or current[0].machine_public_key != self._machine_public_key.hex():
            raise HostedRekeyError("hosted machine key is awaiting rekey")
        facts = current[:2]
        if self._claim_service is None or self._claim_facts != facts:
            self._claim_service = self._first_claim_factory(*facts)
            self._claim_facts = facts
        return self._claim_service

    def status(self) -> str:
        """Report setup status only after the current machine key is bound."""
        return self._claim().status()

    def signed_challenge(self) -> dict[str, Any]:
        """Sign the current challenge after rekey completes."""
        return self._claim().signed_challenge()

    def install_grant(self, envelope: dict[str, Any]) -> None:
        """Install only the current cloud grant through the first-claim verifier."""
        self._claim().install_grant(envelope)

    def claim(self, secret: str, password: str) -> User:
        """Create the initial account only through the verified first claim."""
        return self._claim().claim(secret, password)

    def signed_intent(self, *, now: int | None = None) -> dict[str, Any]:
        """Expose a fresh key-possession proof bound to the current old head."""
        moment = int(time.time()) if now is None else now
        current = self._journal.current()
        head = self._journal.latest()
        if current is None or head is None or head.intent.startswith(("pending:", "claimed:")):
            raise HostedRekeyError("hosted first claim cannot rekey")
        challenge, epoch, grant = current
        if challenge.machine_public_key == self._machine_public_key.hex():
            self._pending = None
            return {"status": "current"}
        if self._pending is not None:
            pending_facts = MachineRekeyIntent.model_validate(self._pending["facts"])
            if (
                pending_facts.previous_head_digest == head.digest
                and pending_facts.previous_head_version == head.version
                and moment < pending_facts.challenge.expires_at
            ):
                return self._pending
        next_challenge = challenge.model_copy(
            update={
                "machine_public_key": self._machine_public_key.hex(),
                "nonce": secrets.token_urlsafe(32),
                "issued_at": moment,
                "expires_at": moment + 300,
            }
        )
        intent = MachineRekeyIntent(
            previous_head_scope=head.scope,
            previous_head_version=head.version,
            previous_head_digest=head.digest,
            previous_challenge_digest=hashlib.sha256(challenge.canonical_bytes()).hexdigest(),
            current_epoch=epoch,
            next_epoch=epoch + int(grant is not None),
            challenge=next_challenge,
        )
        self._pending = sign_rekey_intent(intent, self._machine_signer)
        return self._pending

    def install_rekey(self, envelope: dict[str, Any], *, now: int | None = None) -> None:
        """Advance only the locally pending intent endorsed by the cloud issuer."""
        pending = self._pending
        if pending is None:
            raise HostedRekeyError("hosted rekey was not requested")
        try:
            grant = MachineRekeyGrant.model_validate(envelope["facts"])
            if grant.intent != pending:
                raise HostedRekeyError("hosted rekey intent changed")
            verify_rekey_grant(
                envelope,
                issuer_public_key=self._issuer_public_key,
                expected=grant,
                now=now,
            )
            self._journal.rekey_challenge(
                envelope,
                issuer_public_key=self._issuer_public_key,
                tenant_id=self._tenant_id,
                audit_sink=self._audit_sink,
                now=now,
            )
            self._pending = None
            self._claim_service = None
            self._claim_facts = None
        except (KeyError, TypeError, ValueError, HostedJournalError, MachineRekeyError) as exc:
            raise HostedRekeyError("hosted machine rekey refused") from exc
