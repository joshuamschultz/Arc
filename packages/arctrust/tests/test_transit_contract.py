"""One custody-transit contract, run against every implementation.

The same assertions hold for the local :class:`FileNotaryTransit`, for
:class:`VaultTransit` against a strict fake Vault, and for :class:`VaultTransit`
against a real ``vault server -dev`` (skipped, with the reason, when neither the
``vault`` binary nor the pinned Docker image is present).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from packages.arctrust.tests.vault_dev import DevVault, dev_vault_unavailable_reason
from packages.arctrust.tests.vault_fake import FakeVault

from arctrust.signer import ECDSA_P256, ED25519, FileNotaryTransit, verify_signature
from arctrust.transit_cipher import TransitDecryptError, TransitUnavailableError
from arctrust.vault_transit import VaultTransit, VaultTransitConfig

CIPHER = "connector-credentials"
OPERATOR = "operator"
AAD = b"conn-1\x00client_secret"
_DEV_SKIP = dev_vault_unavailable_reason()


def _vault(addr: str, ca: Path, role_id: str, secret_id: str, algorithm: str, mp: Any) -> Any:
    credentials = ca.parent / "creds"
    credentials.mkdir(exist_ok=True)
    (credentials / "vault-secret-id").write_text(secret_id)
    mp.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    operator_key = "arc-operator" if algorithm == ED25519 else "arc-operator-p256"
    config = VaultTransitConfig(
        addr=addr,
        ca_bundle=str(ca),
        role_id=role_id,
        secret_source="credential:vault-secret-id",
        keys={OPERATOR: operator_key, CIPHER: "arc-connector-credentials"},
        max_retries=0,
        timeout_s=2,
    )
    return VaultTransit(config, algorithm=algorithm)


@pytest.fixture(scope="module")
def fake_vault() -> Iterator[FakeVault]:
    with FakeVault() as vault:
        yield vault


@pytest.fixture(scope="module")
def dev_vault() -> Iterator[DevVault]:
    if _DEV_SKIP:
        pytest.skip(_DEV_SKIP)
    with DevVault() as vault:
        yield vault


@pytest.fixture(params=[ED25519, ECDSA_P256])
def algorithm(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(params=["notary", "vault-fake", "vault-dev"])
def transit(
    request: pytest.FixtureRequest,
    algorithm: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    if request.param == "notary":
        FileNotaryTransit.provision(tmp_path / "ks", OPERATOR, b"\x07" * 32, algorithm)
        yield FileNotaryTransit(tmp_path / "ks", algorithm=algorithm)
        return
    if request.param == "vault-fake":
        fake: FakeVault = request.getfixturevalue("fake_vault")
        args = (fake.addr, fake.ca_bundle, fake.state.role_id, fake.state.secret_id)
    else:
        dev: DevVault = request.getfixturevalue("dev_vault")
        args = (dev.addr, dev.ca_bundle, dev.role_id, dev.secret_id)
    transit = _vault(*args, algorithm, monkeypatch)
    yield transit
    transit.close()


def test_round_trip_under_the_same_aad(transit: Any) -> None:
    sealed = transit.encrypt(CIPHER, b"refresh-token-value", aad=AAD)
    assert b"refresh-token-value" not in sealed.encode()
    assert transit.decrypt(CIPHER, sealed, aad=AAD) == b"refresh-token-value"


def test_every_seal_is_fresh(transit: Any) -> None:
    assert transit.encrypt(CIPHER, b"v", aad=AAD) != transit.encrypt(CIPHER, b"v", aad=AAD)


def test_aad_swap_is_refused(transit: Any) -> None:
    sealed = transit.encrypt(CIPHER, b"secret", aad=AAD)
    with pytest.raises(TransitDecryptError):
        transit.decrypt(CIPHER, sealed, aad=b"conn-2\x00client_secret")


def test_tampered_ciphertext_is_refused(transit: Any) -> None:
    sealed = transit.encrypt(CIPHER, b"secret-value-long-enough", aad=AAD)
    head, body = sealed[:-6], sealed[-6:]
    flipped = "A" if body[0] != "A" else "B"
    with pytest.raises(TransitDecryptError):
        transit.decrypt(CIPHER, head + flipped + body[1:], aad=AAD)


def test_foreign_envelope_is_refused(transit: Any) -> None:
    with pytest.raises(TransitDecryptError):
        transit.decrypt(CIPHER, "xc1:not-from-this-transit", aad=AAD)


def test_signature_verifies_under_the_served_public_key(transit: Any, algorithm: str) -> None:
    signature = transit.sign(OPERATOR, b"checkpoint head")
    public = transit.public_key(OPERATOR)
    assert verify_signature(algorithm, b"checkpoint head", signature, public)
    assert not verify_signature(algorithm, b"another head", signature, public)


def test_unknown_key_reference_is_unavailable_not_minted(transit: Any) -> None:
    sealed = transit.encrypt(CIPHER, b"secret", aad=AAD)
    with pytest.raises(TransitUnavailableError):
        transit.decrypt("no-such-key", sealed, aad=AAD)
