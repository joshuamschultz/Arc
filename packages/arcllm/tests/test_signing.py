"""Tests for request signing — asymmetric attestation and canonical serialization."""

import os
from unittest.mock import patch

import pytest
from arctrust.keypair import generate_keypair
from arctrust.signer import ECDSA_P256, ED25519, InProcessSigner, verify_signature

from arcllm._signing import canonical_payload, create_signer
from arcllm.exceptions import ArcLLMConfigError
from arcllm.types import Message, Tool


def _seed_hex() -> str:
    return generate_keypair().private_key.hex()


# ---------------------------------------------------------------------------
# Asymmetric attestation — verifies with the PUBLIC KEY ONLY (REQ-001)
# ---------------------------------------------------------------------------


class TestAsymmetricAttestation:
    def test_signature_verifies_with_public_key_only(self):
        """The verifier holds no private material — non-repudiation (AU-10)."""
        seed_hex = _seed_hex()
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": seed_hex}):
            signer = create_signer("ed25519", "TEST_SIGNING_KEY")
        payload = canonical_payload([Message(role="user", content="hi")], None, "claude-3")
        signature = signer.sign(payload)
        public_key = signer.public_key
        # Only the public key is needed to verify; a different message fails.
        assert verify_signature(ED25519, payload, signature, public_key)
        assert not verify_signature(ED25519, b"tampered", signature, public_key)

    def test_ecdsa_p256_selected_by_config(self):
        seed_hex = _seed_hex()
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": seed_hex}):
            signer = create_signer("ecdsa-p256", "TEST_SIGNING_KEY")
        assert signer.algorithm == ECDSA_P256
        payload = canonical_payload([Message(role="user", content="hi")], None, "m")
        assert verify_signature(ECDSA_P256, payload, signer.sign(payload), signer.public_key)

    def test_returns_arctrust_signer(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": _seed_hex()}):
            signer = create_signer("ed25519", "TEST_SIGNING_KEY")
        assert isinstance(signer, InProcessSigner)


# ---------------------------------------------------------------------------
# canonical_payload
# ---------------------------------------------------------------------------


class TestCanonicalPayload:
    def test_deterministic_serialization(self):
        messages = [Message(role="user", content="hello")]
        tools = [
            Tool(
                name="calc",
                description="Calculate",
                parameters={"type": "object", "properties": {"x": {"type": "number"}}},
            )
        ]
        p1 = canonical_payload(messages, tools, "claude-3")
        p2 = canonical_payload(messages, tools, "claude-3")
        assert p1 == p2

    def test_key_ordering(self):
        """Keys should be sorted for determinism."""
        messages = [Message(role="user", content="test")]
        payload = canonical_payload(messages, None, "model-a")
        payload_str = payload.decode("utf-8")
        # "messages" comes before "model" comes before "tools" alphabetically
        assert payload_str.index('"messages"') < payload_str.index('"model"')
        assert payload_str.index('"model"') < payload_str.index('"tools"')

    def test_none_tools(self):
        messages = [Message(role="user", content="test")]
        payload = canonical_payload(messages, None, "model-a")
        payload_str = payload.decode("utf-8")
        assert '"tools":[]' in payload_str

    def test_returns_bytes(self):
        messages = [Message(role="user", content="test")]
        payload = canonical_payload(messages, None, "model")
        assert isinstance(payload, bytes)

    def test_different_model_different_payload(self):
        messages = [Message(role="user", content="test")]
        p1 = canonical_payload(messages, None, "model-a")
        p2 = canonical_payload(messages, None, "model-b")
        assert p1 != p2


# ---------------------------------------------------------------------------
# create_signer factory
# ---------------------------------------------------------------------------


class TestCreateSigner:
    def test_missing_signing_key_env(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ArcLLMConfigError, match="not set"):
                create_signer("ed25519", "MISSING_KEY_ENV")

    def test_hmac_algorithm_rejected(self):
        """HMAC is deleted — selecting it is a hard config error."""
        with patch.dict(os.environ, {"TEST_KEY": _seed_hex()}):
            with pytest.raises(ArcLLMConfigError, match="Unsupported"):
                create_signer("hmac-sha256", "TEST_KEY")

    def test_unknown_algorithm(self):
        with patch.dict(os.environ, {"TEST_KEY": _seed_hex()}):
            with pytest.raises(ArcLLMConfigError, match="Unsupported"):
                create_signer("rsa-2048", "TEST_KEY")

    def test_non_hex_seed_rejected(self):
        with patch.dict(os.environ, {"TEST_KEY": "not-a-hex-seed"}):
            with pytest.raises(ArcLLMConfigError, match="hex-encoded"):
                create_signer("ed25519", "TEST_KEY")

    def test_wrong_length_seed_rejected(self):
        with patch.dict(os.environ, {"TEST_KEY": "abcd"}):
            with pytest.raises(ArcLLMConfigError, match="32 bytes"):
                create_signer("ed25519", "TEST_KEY")
