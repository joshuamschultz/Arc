"""Actual TLS Vault provider evidence with disposable keys, policies, and storage."""

from __future__ import annotations

import hashlib
import time

import pytest
from packages.arctrust.tests.integration.vault_fixture import DisposableVault, docker_available

from arctrust.monotonic import AnchorUnavailableError
from arctrust.signer import SignerError, verify_signature
from arctrust.transit_http import VaultTransitHTTP
from arctrust.vault_anchor import VaultKVAnchor
from arctrust.vault_cipher import VaultCipher, VaultCipherError

pytestmark = pytest.mark.requires_docker


@pytest.fixture(scope="module")
def vault() -> DisposableVault:
    if not docker_available():
        pytest.skip("Docker daemon unavailable")
    with DisposableVault() as provider:
        yield provider


class Once:
    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.used = False

    def consume(self, scope: str) -> bool:
        if self.used or scope != self.scope:
            return False
        self.used = True
        return True


def test_real_transit_seal_anchor_policy_and_restart(vault: DisposableVault) -> None:
    issuer_token = vault.token("issuer")
    cipher_token = vault.token("cipher")
    anchor_token = vault.token("anchor")
    with (
        vault.client(issuer_token) as issuer_client,
        vault.client(cipher_token) as cipher_client,
        vault.client(anchor_token) as anchor_client,
        vault.client(anchor_token) as second_anchor_client,
    ):
        issuer = VaultTransitHTTP(issuer_client)
        public = issuer.create_user_key("arc-user-integration")
        message = b"disposable-account-proof"
        signature = issuer.sign("arc-user-integration", message)
        assert verify_signature("ed25519", message, signature, public)

        cipher = VaultCipher(
            cipher_client,
            mount="transit",
            key="account-seal",
            scope="account-test",
            record_id="users",
        )
        sealed = cipher.seal(b"disposable account state")
        assert cipher.open(sealed) == b"disposable account state"
        other_scope = VaultCipher(
            cipher_client,
            mount="transit",
            key="account-seal",
            scope="other-test",
            record_id="users",
        )
        with pytest.raises(VaultCipherError):
            other_scope.open(sealed)

        anchor = VaultKVAnchor(
            anchor_client,
            mount="anchor",
            record="users",
            bootstrap_authority=Once("anchor/users"),
        )
        stale = VaultKVAnchor(second_anchor_client, mount="anchor", record="users")
        assert anchor.latest() is None
        first = anchor.compare_and_advance(None, hashlib.sha256(b"one").hexdigest(), "create")
        assert first.version == 1
        second = anchor.compare_and_advance(first, hashlib.sha256(b"two").hexdigest(), "update")
        assert second.version == 2
        assert (
            anchor_client.post(
                "/v1/anchor/data/users", json={"data": {"digest": "unwitnessed"}}
            ).status_code
            == 400
        )
        with pytest.raises(AnchorUnavailableError, match="stale"):
            stale.compare_and_advance(first, hashlib.sha256(b"stale").hexdigest(), "stale")

        # The application tokens lack export, deletion, and KV reset authority.
        assert (
            issuer_client.get("/v1/transit/export/signing-key/arc-user-integration").status_code
            == 403
        )
        assert issuer_client.delete("/v1/transit/keys/arc-user-integration").status_code == 403
        assert anchor_client.delete("/v1/anchor/metadata/users").status_code == 403
        assert (
            anchor_client.post("/v1/anchor/config", json={"cas_required": False}).status_code
            == 403
        )

    vault.restart()
    with (
        vault.client(issuer_token) as issuer_client,
        vault.client(cipher_token) as cipher_client,
        vault.client(anchor_token) as anchor_client,
    ):
        assert VaultTransitHTTP(issuer_client).public_key("arc-user-integration") == public
        assert (
            VaultCipher(
                cipher_client,
                mount="transit",
                key="account-seal",
                scope="account-test",
                record_id="users",
            ).open(sealed)
            == b"disposable account state"
        )
        assert VaultKVAnchor(anchor_client, mount="anchor", record="users").latest() == second


def test_expired_scoped_token_refuses_signing(vault: DisposableVault) -> None:
    token = vault.token("issuer", ttl="2s")
    with vault.client(token) as client:
        issuer = VaultTransitHTTP(client)
        assert len(issuer.create_user_key("arc-user-expiry")) == 32
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if client.get("/v1/transit/keys/arc-user-expiry").status_code == 403:
                break
            time.sleep(0.2)
        else:
            pytest.fail("short-lived Vault token did not expire")
        with pytest.raises(SignerError, match="unavailable"):
            issuer.sign("arc-user-expiry", b"expired")
