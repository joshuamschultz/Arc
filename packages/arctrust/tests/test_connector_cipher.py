"""P18-2 §8.1 — the connector credential cipher (XChaCha20-Poly1305, AAD-bound)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from arctrust import ArcTrustFipsError, OperatorKey
from arctrust.audit_cipher import derive_record_key
from arctrust.connector_cipher import (
    ConnectorSecretCipher,
    CredentialSealError,
    _derive_connector_key,
)


def _key(tmp_path: Path, name: str = "op") -> OperatorKey:
    return OperatorKey.load(tmp_path / name / "operator.key", generate_if_absent=True)


@pytest.fixture
def cipher(tmp_path: Path) -> ConnectorSecretCipher:
    return ConnectorSecretCipher.for_operator_key(_key(tmp_path))


def _flip(sealed: str, index: int) -> str:
    prefix, _, body = sealed.rpartition(".")
    raw = bytearray(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    raw[index] ^= 0x01
    return f"{prefix}.{base64.urlsafe_b64encode(bytes(raw)).decode().rstrip('=')}"


def test_roundtrip_and_tamper_fails(cipher: ConnectorSecretCipher) -> None:
    sealed = cipher.seal(b"refresh-abc", scope="blackarc", slot="refresh_token")
    assert sealed.startswith("v1.xc.")
    assert cipher.open(sealed, scope="blackarc", slot="refresh_token") == b"refresh-abc"
    # nonce (first byte), ciphertext (byte 24), tag (last byte)
    for index in (0, 24, -1):
        with pytest.raises(CredentialSealError):
            cipher.open(_flip(sealed, index), scope="blackarc", slot="refresh_token")


def test_aad_binds_scope_and_slot(cipher: ConnectorSecretCipher) -> None:
    sealed = cipher.seal(b"refresh-abc", scope="blackarc", slot="refresh_token")
    with pytest.raises(CredentialSealError):
        cipher.open(sealed, scope="systems", slot="refresh_token")
    with pytest.raises(CredentialSealError):
        cipher.open(sealed, scope="blackarc", slot="client_secret")


def test_key_is_deterministic_and_domain_separated(tmp_path: Path) -> None:
    key = _key(tmp_path)
    assert _derive_connector_key(key.seed) == _derive_connector_key(key.seed)
    assert _derive_connector_key(key.seed) != derive_record_key(key.seed)
    other = _key(tmp_path, "other")
    assert _derive_connector_key(key.seed) != _derive_connector_key(other.seed)
    # Same seed, two cipher instances: one opens what the other sealed.
    first = ConnectorSecretCipher.for_operator_key(key)
    second = ConnectorSecretCipher.for_operator_key(key)
    sealed = first.seal(b"v", scope="a", slot="b")
    assert second.open(sealed, scope="a", slot="b") == b"v"
    with pytest.raises(CredentialSealError):
        ConnectorSecretCipher.for_operator_key(other).open(sealed, scope="a", slot="b")


def test_nonce_is_random_per_seal(cipher: ConnectorSecretCipher) -> None:
    sealed = {cipher.seal(b"same", scope="a", slot="b") for _ in range(1000)}
    assert len(sealed) == 1000


def test_errors_never_echo_material(cipher: ConnectorSecretCipher) -> None:
    sealed = cipher.seal(b"plaintext-marker-123", scope="a", slot="b")
    with pytest.raises(CredentialSealError) as caught:
        cipher.open(sealed, scope="a", slot="c")
    text = f"{caught.value!s} {caught.value!r} {caught.value.args}"
    assert "plaintext-marker-123" not in text
    assert sealed not in text
    assert sealed.rpartition(".")[2][:16] not in text
    with pytest.raises(CredentialSealError) as garbage:
        cipher.open("v1.xc.!!!not-base64", scope="a", slot="b")
    assert "not-base64" not in str(garbage.value)


def test_refuses_bad_coordinates_and_oversize(cipher: ConnectorSecretCipher) -> None:
    with pytest.raises(CredentialSealError):
        cipher.seal(b"x", scope="Bad Scope", slot="b")
    with pytest.raises(CredentialSealError):
        cipher.seal(b"x" * (64 * 1024 + 1), scope="a", slot="b")
    with pytest.raises(CredentialSealError):
        cipher.open("v1.tr.anything", scope="a", slot="b")


def test_kind_is_xc1(cipher: ConnectorSecretCipher) -> None:
    assert cipher.kind == "xc1"


def test_fips_mode_refuses_in_process_cipher(tmp_path: Path) -> None:
    with pytest.raises(ArcTrustFipsError):
        ConnectorSecretCipher.for_operator_key(_key(tmp_path), require_fips=True)


def test_derived_key_is_not_exposed(cipher: ConnectorSecretCipher) -> None:
    public = [name for name in dir(cipher) if not name.startswith("_")]
    assert sorted(public) == ["PERSON", "for_operator_key", "kind", "open", "seal"]


def test_operator_key_for_reads_in_process_and_refuses_transit(tmp_path: Path) -> None:
    from arctrust import operator_key_for
    from arctrust.operator_resolver import MachineSecurity

    in_process = MachineSecurity(operator_key_dir=str(tmp_path / "keys"))
    with pytest.raises(FileNotFoundError):
        operator_key_for(in_process)
    minted = operator_key_for(in_process, bootstrap=True)
    assert minted is not None
    again = operator_key_for(in_process)
    assert again is not None and again.seed == minted.seed
    assert operator_key_for(MachineSecurity(custody="vault_transit")) is None
