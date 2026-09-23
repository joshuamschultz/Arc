"""Signed account authority against disposable TLS Vault and narrow real ACLs."""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from nacl.signing import SigningKey
from packages.arctrust.tests.integration.vault_fixture import DisposableVault, docker_available

from arctrust.audit import WormSink, verify_chain
from arctrust.authority import open_account_authority
from arctrust.authority_config import DeploymentAuthorityConfig, sign_authority_config
from arctrust.signer import VaultSigner
from arctrust.transit_http import VaultTransitHTTP
from arctrust.vault_anchor import VaultKVAnchor
from arctrust.vault_lease import (
    Capability,
    CapabilityGrant,
    VaultLeaseError,
    open_vault_lease,
    sign_capability_grant,
)

pytestmark = pytest.mark.requires_docker


class FixtureCredentials:
    def __init__(self, vault):
        self.vault = vault

    def authorize(self, client, *, subject, policy_name, capability):
        if subject != "dgx-workload" or not isinstance(capability, Capability):
            raise PermissionError("fixture identity refused")
        client.headers["X-Vault-Token"] = self.vault.token_for_policy(policy_name, ttl="8s")


class ActorVerifier:
    def authenticate(self, proof, *, action):
        if proof is not self or action != "users.mutate":
            raise PermissionError("actor proof refused")
        return "did:arc:acme:user/fixture-operator"


class OnceUsers:
    def __init__(self, scope):
        self.scope = scope
        self.used = False

    def consume(self, scope):
        if self.used or scope != self.scope:
            return False
        self.used = True
        return True


def _grant(config, key, capability):
    grant = CapabilityGrant(
        deployment_id=config.deployment_id, tenant_id=config.tenant_id,
        config_revision=config.revision, config_digest=config.digest,
        capability=capability, subject="dgx-workload",
        expires_at=int(time.time()) + 90,
    )
    return sign_capability_grant(grant, lambda data: key.sign(data).signature)


def test_real_authority_policies_renew_revoke_and_restart(tmp_path):
    if not docker_available():
        pytest.skip("Docker daemon unavailable")
    with DisposableVault() as vault:
        vault.configure_account_namespace("acme", "dgx")
        ca_pem = vault.cert_path.read_bytes()
        config = DeploymentAuthorityConfig(
            deployment_id="dgx", tenant_id="acme", revision=1,
            vault_url=vault.base_url, vault_ca_sha256=hashlib.sha256(ca_pem).hexdigest(),
        )
        config_key, grant_key = SigningKey.generate(), SigningKey.generate()
        config_path = tmp_path / "authority.json"
        config_path.write_text(json.dumps(sign_authority_config(
            config, lambda data: config_key.sign(data).signature,
        )))
        config_path.chmod(0o600)

        # Fixture root performs one-time enrollment; the application sees only
        # a separately scoped read capability for this independent config head.
        vault.seed_config_anchor(config.anchor_mount, config.digest)
        with vault.client(vault.token_for_policy("arc-acme-dgx-config-anchor")) as reader:
            config_anchor = VaultKVAnchor(reader, mount=config.anchor_mount, record="config")
            assert config_anchor.latest().digest == config.digest

            credentials = FixtureCredentials(vault)
            audit_grant = _grant(config, grant_key, Capability.AUDIT_SIGNER)
            audit_lease = open_vault_lease(
                config, audit_grant, trusted_grant_key=bytes(grant_key.verify_key),
                credential_provider=credentials, ca_pem=ca_pem,
            )
            transit = VaultTransitHTTP(
                audit_lease._adapter_client(), mount=config.transit_mount,
            )
            signer = VaultSigner(transit, "audit-signing")
            audit_path = tmp_path / "audit.worm"
            sink = WormSink(audit_path, signer)
            actor = ActorVerifier()
            signed_grants = {
                role: _grant(config, grant_key, role)
                for role in (Capability.ISSUER, Capability.CIPHER, Capability.ANCHOR)
            }
            authority = open_account_authority(
                config_path, trusted_config_key=bytes(config_key.verify_key),
                expected_deployment="dgx", expected_tenant="acme", config_anchor=config_anchor,
                signed_grants=signed_grants, trusted_grant_key=bytes(grant_key.verify_key),
                credential_provider=credentials, ca_pem=ca_pem,
                audit_sink=sink, strict_audit_sink=sink, actor_verifier=actor,
                users_path=tmp_path / "users.json", bootstrap_authority=OnceUsers(
                    f"{config.anchor_mount}/users"
                ),
            )
            try:
                with pytest.raises(Exception, match="actor"):
                    authority.user_store("did:arc:acme:user/forged")
                store = authority.user_store(actor)
                store.add("operator@example.com", "correct-horse-battery", roles=("operator",))
                issuer_client = authority._leases[Capability.ISSUER]._adapter_client()
                issuer_client._lease_deadline = time.monotonic() + 1
                assert issuer_client.get(
                    f"/v1/{config.transit_mount}/keys/{store.get('operator@example.com').signing_key_ref}"
                ).status_code == 200
                # Force renewal soon, then prove a real Vault request succeeds.
                assert issuer_client._lease_deadline > time.monotonic() + 5
                vault.restart()
                assert authority.user_store(actor).get("operator@example.com") is not None
                assert verify_chain(audit_path, signer.public_key)
                with vault.client(vault.token_for_policy("arc-acme-dgx-issuer")) as scoped:
                    assert scoped.delete(
                        f"/v1/{config.transit_mount}/keys/audit-signing"
                    ).status_code == 403
                    assert scoped.get(
                        f"/v1/{config.anchor_mount}/data/users"
                    ).status_code == 403
            finally:
                authority.close()
                sink.close()
                audit_lease.close()
            with pytest.raises(VaultLeaseError):
                issuer_client.get(f"/v1/{config.transit_mount}/keys/account-seal")
