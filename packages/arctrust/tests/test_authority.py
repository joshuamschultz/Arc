"""Authority refuses unverified actors and incomplete scoped capabilities."""

from __future__ import annotations

import pytest

from arctrust.authority import AccountAuthority, AccountAuthorityError
from arctrust.authority_config import DeploymentAuthorityConfig


class ActorVerifier:
    def authenticate(self, proof, *, action):
        if proof != "trusted-request" or action != "users.mutate":
            raise PermissionError("unverified actor")
        return "did:arc:acme:user/verified"


def test_actor_verification_precedes_user_store_construction():
    authority = AccountAuthority.__new__(AccountAuthority)
    authority._actor_verifier = ActorVerifier()
    authority._config = DeploymentAuthorityConfig(
        deployment_id="dgx", tenant_id="acme", revision=1,
        vault_url="https://vault.example", vault_ca_sha256="a" * 64,
    )
    with pytest.raises(AccountAuthorityError, match="actor"):
        authority._actor_did("client-chosen-did")
    assert authority._actor_did("trusted-request") == "did:arc:acme:user/verified"
