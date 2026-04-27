"""Tests for arctrust.keypair — Ed25519 key generation, signing, verification."""

from __future__ import annotations

import pytest

from arctrust.keypair import KeyPair, generate_keypair, sign, verify


class TestGenerateKeypair:
    def test_returns_keypair(self) -> None:
        kp = generate_keypair()
        assert isinstance(kp, KeyPair)

    def test_public_key_is_32_bytes(self) -> None:
        kp = generate_keypair()
        assert len(kp.public_key) == 32

    def test_private_key_is_32_bytes(self) -> None:
        kp = generate_keypair()
        assert len(kp.private_key) == 32

    def test_two_generated_keys_differ(self) -> None:
        kp1 = generate_keypair()
        kp2 = generate_keypair()
        assert kp1.public_key != kp2.public_key
        assert kp1.private_key != kp2.private_key

    def test_from_seed_is_deterministic(self) -> None:
        """Constructing KeyPair from the same seed gives the same keys."""
        kp1 = generate_keypair()
        kp2 = KeyPair.from_seed(kp1.private_key)
        assert kp2.public_key == kp1.public_key
        assert kp2.private_key == kp1.private_key


class TestSign:
    def test_returns_64_bytes(self) -> None:
        kp = generate_keypair()
        sig = sign(b"hello", kp.private_key)
        assert len(sig) == 64

    def test_same_message_same_signature(self) -> None:
        """Ed25519 is deterministic — same key + message → same sig."""
        kp = generate_keypair()
        s1 = sign(b"msg", kp.private_key)
        s2 = sign(b"msg", kp.private_key)
        assert s1 == s2

    def test_different_message_different_signature(self) -> None:
        kp = generate_keypair()
        s1 = sign(b"msg1", kp.private_key)
        s2 = sign(b"msg2", kp.private_key)
        assert s1 != s2

    def test_empty_message_is_signable(self) -> None:
        kp = generate_keypair()
        sig = sign(b"", kp.private_key)
        assert len(sig) == 64


class TestVerify:
    def test_valid_signature_returns_true(self) -> None:
        kp = generate_keypair()
        sig = sign(b"hello", kp.private_key)
        assert verify(b"hello", sig, kp.public_key)

    def test_wrong_message_returns_false(self) -> None:
        kp = generate_keypair()
        sig = sign(b"hello", kp.private_key)
        assert not verify(b"wrong", sig, kp.public_key)

    def test_wrong_key_returns_false(self) -> None:
        kp1 = generate_keypair()
        kp2 = generate_keypair()
        sig = sign(b"hello", kp1.private_key)
        assert not verify(b"hello", sig, kp2.public_key)

    def test_tampered_signature_returns_false(self) -> None:
        kp = generate_keypair()
        sig = bytearray(sign(b"hello", kp.private_key))
        sig[0] ^= 0xFF  # flip first byte
        assert not verify(b"hello", bytes(sig), kp.public_key)

    def test_truncated_signature_returns_false(self) -> None:
        kp = generate_keypair()
        sig = sign(b"hello", kp.private_key)
        assert not verify(b"hello", sig[:32], kp.public_key)

    def test_empty_signature_returns_false(self) -> None:
        kp = generate_keypair()
        assert not verify(b"hello", b"", kp.public_key)

    def test_empty_message_verify_roundtrip(self) -> None:
        kp = generate_keypair()
        sig = sign(b"", kp.private_key)
        assert verify(b"", sig, kp.public_key)


class TestKeypairRoundtrip:
    def test_sign_and_verify_via_keypair_object(self) -> None:
        kp = generate_keypair()
        message = b"full roundtrip test"
        sig = sign(message, kp.private_key)
        assert verify(message, sig, kp.public_key)

    def test_invalid_private_key_length_raises(self) -> None:
        with pytest.raises(ValueError):
            sign(b"msg", b"too-short")

    def test_invalid_public_key_length_returns_false(self) -> None:
        kp = generate_keypair()
        sig = sign(b"msg", kp.private_key)
        # Too short public key → False (not raise)
        result = verify(b"msg", sig, b"bad-key")
        assert not result
