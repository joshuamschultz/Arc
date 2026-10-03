"""Abuse cases for Vault Transit custody (adversarial battery).

An attacker who can stand up a look-alike Vault, answer with a redirect, swap
the key behind a pinned name, weaken a key's settings, or read the deployment's
files must not get a credential, a token, a signature it can choose, or a
silent fallback.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from nacl.signing import SigningKey
from packages.arctrust.tests.vault_fake import FakeVault

from arctrust.audit import AuditEvent
from arctrust.signer import SignerError
from arctrust.transit_cipher import TransitDecryptError
from arctrust.vault_transit import (
    VaultTransit,
    VaultTransitConfig,
    VaultTransitUnavailableError,
)

AAD = b"conn_1\x00refresh_token"


@pytest.fixture
def fake() -> Iterator[FakeVault]:
    with FakeVault() as vault:
        yield vault


def _transit(fake: FakeVault, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> VaultTransit:
    monkeypatch.setenv("ARC_ABUSE_SECRET", fake.state.secret_id)
    values: dict[str, Any] = {
        "addr": fake.addr,
        "ca_bundle": str(fake.ca_bundle),
        "role_id": fake.state.role_id,
        "secret_source": "env:ARC_ABUSE_SECRET",
        "max_retries": 0,
    }
    values.update(overrides)
    return VaultTransit(VaultTransitConfig(**values), sleep=lambda _s: None)


def test_lookalike_vault_with_another_ca_never_receives_the_secret_id(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    with FakeVault() as rogue:
        transit = _transit(rogue, monkeypatch, ca_bundle=str(fake.ca_bundle))
        with pytest.raises(VaultTransitUnavailableError):
            transit.encrypt("connector-credentials", b"v", aad=AAD)
        assert rogue.state.calls == []  # TLS refused before any byte of a request


def test_redirect_to_another_server_is_not_followed(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    with FakeVault() as rogue:
        fake.state.redirect_to = rogue.addr
        transit = _transit(fake, monkeypatch)
        with pytest.raises(VaultTransitUnavailableError):
            transit.encrypt("connector-credentials", b"secret", aad=AAD)
        assert rogue.state.calls == []


def test_key_swapped_behind_a_pinned_name_cannot_sign_for_the_operator(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit = _transit(fake, monkeypatch)
    transit.public_key("operator")  # pinned
    fake.state.keys["arc-operator"].material = SigningKey.generate()  # attacker's key
    with pytest.raises(SignerError, match="pinned"):
        transit.sign("operator", b"attacker-chosen checkpoint")


@pytest.mark.parametrize("flag", ["exportable", "allow_plaintext_backup", "derived"])
def test_weakened_cipher_key_is_never_used(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    fake.add_key("arc-connector-credentials", "aes256-gcm96", **{flag: True})
    transit = _transit(fake, monkeypatch)
    with pytest.raises(VaultTransitUnavailableError):
        transit.encrypt("connector-credentials", b"secret", aad=AAD)
    assert fake.calls("/encrypt/") == 0


def test_credential_transplanted_to_another_connection_does_not_open(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit = _transit(fake, monkeypatch)
    sealed = transit.encrypt("connector-credentials", b"victim-token", aad=AAD)
    with pytest.raises(TransitDecryptError):
        transit.decrypt("connector-credentials", sealed, aad=b"conn_2\x00refresh_token")


def test_signing_key_cannot_be_used_as_a_cipher(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit = _transit(fake, monkeypatch, keys={"connector-credentials": "arc-operator"})
    with pytest.raises(VaultTransitUnavailableError):
        transit.encrypt("connector-credentials", b"secret", aad=AAD)


def test_outage_never_falls_back(fake: FakeVault, monkeypatch: pytest.MonkeyPatch) -> None:
    transit = _transit(fake, monkeypatch)
    sealed = transit.encrypt("connector-credentials", b"v", aad=AAD)
    fake.state.fail_statuses = [503] * 10
    with pytest.raises(VaultTransitUnavailableError):
        transit.decrypt("connector-credentials", sealed, aad=AAD)
    with pytest.raises(VaultTransitUnavailableError):
        transit.sign("operator", b"m")


def test_token_and_secret_never_touch_the_deployment_tree(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    transit = _transit(fake, monkeypatch)
    transit.decrypt(
        "connector-credentials", transit.encrypt("connector-credentials", b"v", aad=AAD), aad=AAD
    )
    transit.sign("operator", b"m")
    secrets_in_play = [fake.state.secret_id.encode(), *(t.encode() for t in fake.state.tokens)]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            body = path.read_bytes()
            assert not any(secret in body for secret in secrets_in_play), path


def test_audit_sink_that_signs_through_the_transit_does_not_recurse(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: dict[str, VaultTransit] = {}
    written: list[AuditEvent] = []

    class SigningSink:
        def write(self, event: AuditEvent) -> None:
            holder["t"].sign("operator", event.action.encode())
            written.append(event)

    monkeypatch.setenv("ARC_ABUSE_SECRET", fake.state.secret_id)
    holder["t"] = VaultTransit(
        VaultTransitConfig(
            addr=fake.addr,
            ca_bundle=str(fake.ca_bundle),
            role_id=fake.state.role_id,
            secret_source="env:ARC_ABUSE_SECRET",
        ),
        audit_sink=SigningSink(),
    )
    holder["t"].encrypt("connector-credentials", b"v", aad=AAD)
    assert any(event.action == "custody.transit.encrypt" for event in written)
