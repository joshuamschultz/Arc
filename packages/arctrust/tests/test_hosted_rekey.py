"""A new process resumes only through a cloud-endorsed new-key intent."""

from __future__ import annotations

import pytest
from nacl.signing import SigningKey
from packages.arctrust.tests.test_hosted_claim import Authority
from packages.arctrust.tests.test_hosted_journal import _new

from arctrust.hosted_claim import HostedFirstClaim
from arctrust.hosted_rekey import HostedRekeyCoordinator, HostedRekeyError
from arctrust.machine_rekey import MachineRekeyGrant, sign_rekey_grant


def test_restarted_machine_rekeys_once_and_cloud_grant_is_exact() -> None:
    journal, _old_service, _old_machine, issuer, users, proof, audit = _new()
    challenge, epoch, _ = journal.current()
    new_machine = SigningKey.generate()
    coordinator = HostedRekeyCoordinator(
        journal=journal,
        machine_public_key=bytes(new_machine.verify_key),
        machine_signer=lambda message: new_machine.sign(message).signature,
        issuer_public_key=bytes(issuer.verify_key),
        tenant_id="acme",
        audit_sink=audit,
        first_claim_factory=lambda current, epoch: HostedFirstClaim(
            challenge=current,
            expected_epoch=epoch,
            issuer_public_key=bytes(issuer.verify_key),
            anchor=journal,
            authority=Authority(users, proof),
            actor_proof=proof,
            tenant_id="acme",
            audit_sink=audit,
            machine_signer=lambda message: new_machine.sign(message).signature,
        ),
    )
    moment = challenge.issued_at + 30
    intent = coordinator.signed_intent(now=moment)
    assert coordinator.signed_intent(now=moment + 1) == intent
    assert intent["facts"]["current_epoch"] == epoch
    grant = MachineRekeyGrant(
        intent=intent,
        customer_id="cus_123",
        subscription_id="sub_123",
        purpose="machine-rekey",
    )
    envelope = sign_rekey_grant(grant, lambda message: issuer.sign(message).signature, now=moment)
    changed = {**envelope, "facts": {**envelope["facts"], "customer_id": "cus_other"}}
    with pytest.raises(HostedRekeyError):
        coordinator.install_rekey(changed, now=moment)
    assert journal.current()[0] == challenge
    coordinator.install_rekey(envelope, now=moment)
    assert journal.current()[0].machine_public_key == bytes(new_machine.verify_key).hex()
    assert coordinator.signed_intent(now=moment) == {"status": "current"}
    assert coordinator.status() == "awaiting_grant"
    with pytest.raises(HostedRekeyError):
        coordinator.install_rekey(envelope, now=moment)
