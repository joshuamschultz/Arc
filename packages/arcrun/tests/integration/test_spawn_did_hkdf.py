"""Integration tests for per-child DID derivation via HKDF.

Verifies:
1. Child DID derived deterministically from parent + nonce
2. Different nonces → different DIDs
3. Same nonce → same DID (reproducibility)
4. Different parent keys → different DIDs
5. DID format validity
"""

from __future__ import annotations

import pytest

from arcrun.builtins.spawn import ChildIdentity, derive_child_identity


class TestHKDFDIDDerivation:
    def test_deterministic_same_nonce(self) -> None:
        """Same parent + same nonce always produces same child DID."""
        sk = b"\xAA" * 32
        id1 = derive_child_identity(sk, "nonce-stable", 300)
        id2 = derive_child_identity(sk, "nonce-stable", 300)
        assert id1.did == id2.did
        assert id1.sk_bytes == id2.sk_bytes

    def test_different_nonce_different_did(self) -> None:
        """Different nonces must produce different child DIDs."""
        sk = b"\xBB" * 32
        ids = [derive_child_identity(sk, f"nonce-{i}", 300) for i in range(10)]
        dids = [i.did for i in ids]
        # All DIDs must be unique
        assert len(set(dids)) == 10

    def test_different_parent_sk_different_did(self) -> None:
        """Different parent keys with same nonce produce different child DIDs."""
        nonce = "same-nonce"
        id1 = derive_child_identity(b"\x01" * 32, nonce, 300)
        id2 = derive_child_identity(b"\x02" * 32, nonce, 300)
        assert id1.did != id2.did
        assert id1.sk_bytes != id2.sk_bytes

    def test_did_format_prefix(self) -> None:
        """All derived DIDs must start with the expected prefix."""
        identity = derive_child_identity(b"\x00" * 32, "n", 300)
        assert identity.did.startswith("did:arc:delegate:child/")

    def test_did_hash_segment_length(self) -> None:
        """The hash segment in the DID should be 8 hex characters."""
        identity = derive_child_identity(b"\x55" * 32, "n2", 300)
        prefix = "did:arc:delegate:child/"
        hash_segment = identity.did[len(prefix):]
        assert len(hash_segment) == 8
        # Must be valid hex
        int(hash_segment, 16)

    def test_sk_bytes_always_32_bytes(self) -> None:
        """Derived signing key bytes must always be exactly 32 bytes."""
        for i in range(5):
            identity = derive_child_identity(b"\x11" * 32, f"n{i}", 300)
            assert len(identity.sk_bytes) == 32

    def test_ttl_stored_correctly(self) -> None:
        identity = derive_child_identity(b"\x22" * 32, "n", 600)
        assert identity.ttl_s == 600

    def test_different_ttl_same_nonce_same_did(self) -> None:
        """TTL does NOT affect key derivation — same parent + same nonce = same DID."""
        sk = b"\x33" * 32
        id1 = derive_child_identity(sk, "nonce-ttl", 300)
        id2 = derive_child_identity(sk, "nonce-ttl", 600)
        assert id1.did == id2.did
        assert id1.sk_bytes == id2.sk_bytes

    def test_zero_parent_key_produces_valid_did(self) -> None:
        """Even a zero key produces a valid DID (no crash, no empty string)."""
        identity = derive_child_identity(b"\x00" * 32, "nonce-zero", 300)
        assert len(identity.did) > len("did:arc:delegate:child/")

    def test_sibling_dids_are_all_unique(self) -> None:
        """Simulates 5 sibling children from same parent — all DIDs must differ."""
        import uuid

        sk = b"\xDE" * 32
        spawn_ids = [str(uuid.uuid4()) for _ in range(5)]
        identities = [derive_child_identity(sk, sid, 300) for sid in spawn_ids]
        dids = [i.did for i in identities]
        assert len(set(dids)) == 5

    def test_child_identity_model_validity(self) -> None:
        """ChildIdentity Pydantic model can be constructed and serialized."""
        identity = derive_child_identity(b"\xFF" * 32, "model-test", 300)
        assert isinstance(identity, ChildIdentity)
        # Can round-trip through dict
        data = identity.model_dump()
        assert "did" in data
        assert "sk_bytes" in data
        assert "ttl_s" in data
        restored = ChildIdentity(**data)
        assert restored.did == identity.did
