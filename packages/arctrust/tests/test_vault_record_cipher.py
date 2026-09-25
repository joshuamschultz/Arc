"""Queue record envelopes keep authenticated scope behind byte-key custody."""

import base64

import pytest

from arctrust.vault_record_cipher import VaultRecordCipher


class _Bytes:
    def seal(self, payload: bytes) -> str:
        return "vault:v1:" + base64.b64encode(payload).decode()

    def open(self, sealed: str) -> bytes:
        return base64.b64decode(sealed.removeprefix("vault:v1:"), validate=True)


def test_record_cipher_binds_tenant_scope_and_purpose() -> None:
    cipher = _Bytes()
    queue = VaultRecordCipher(
        cipher,
        tenant_id="tenant-a",
        journal_scope="queue/tenant-a",
        purpose="queue.metadata",
    )
    sealed = queue.seal({"extra": {"state": "queued"}})
    assert set(sealed["extra"]) == {"arc.audit.sealed"}
    assert queue.unseal(sealed)["extra"] == {"state": "queued"}
    foreign = VaultRecordCipher(
        cipher,
        tenant_id="tenant-b",
        journal_scope="queue/tenant-a",
        purpose="queue.metadata",
    )
    with pytest.raises(ValueError, match="record envelope"):
        foreign.unseal(sealed)
    wrong_purpose = VaultRecordCipher(
        cipher,
        tenant_id="tenant-a",
        journal_scope="queue/tenant-a",
        purpose="queue.payload",
    )
    with pytest.raises(ValueError, match="record envelope"):
        wrong_purpose.unseal(sealed)
