"""Capability grants select one policy and leased clients renew or revoke."""

from __future__ import annotations

import time

import pytest
from nacl.signing import SigningKey

from arctrust.authority_config import DeploymentAuthorityConfig
from arctrust.vault_lease import (
    Capability,
    CapabilityGrant,
    VaultLeaseError,
    open_vault_lease,
    sign_capability_grant,
)


def _config():
    return DeploymentAuthorityConfig(
        deployment_id="dgx",
        tenant_id="acme",
        revision=1,
        vault_url="https://vault.example",
        vault_ca_sha256="a" * 64,
    )


def test_policy_comes_only_from_signed_grant():
    key = SigningKey.generate()
    config = _config()
    grant = CapabilityGrant(
        deployment_id="dgx",
        tenant_id="acme",
        config_revision=1,
        config_digest=config.digest,
        capability=Capability.ISSUER,
        subject="dgx-workload",
        expires_at=int(time.time()) + 60,
    )
    envelope = sign_capability_grant(grant, lambda data: key.sign(data).signature)
    assert grant.policy_name == "arc-acme-dgx-issuer"
    with pytest.raises(VaultLeaseError):
        open_vault_lease(
            config,
            envelope,
            trusted_grant_key=bytes(SigningKey.generate().verify_key),
            credential_provider=object(),
            ca_pem=b"fake",
        )


def test_grant_refuses_wrong_scope_expiry_and_policy_injection():
    key = SigningKey.generate()
    config = _config()
    grant = CapabilityGrant(
        deployment_id="dgx",
        tenant_id="acme",
        config_revision=1,
        config_digest=config.digest,
        capability=Capability.ANCHOR,
        subject="dgx-workload",
        expires_at=int(time.time()) - 1,
    )
    envelope = sign_capability_grant(grant, lambda data: key.sign(data).signature)
    with pytest.raises(VaultLeaseError):
        open_vault_lease(
            config,
            envelope,
            trusted_grant_key=bytes(key.verify_key),
            credential_provider=object(),
            ca_pem=b"fake",
        )
    envelope["grant"]["policy_name"] = "root"
    with pytest.raises(VaultLeaseError):
        open_vault_lease(
            config,
            envelope,
            trusted_grant_key=bytes(key.verify_key),
            credential_provider=object(),
            ca_pem=b"fake",
        )
