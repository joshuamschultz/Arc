"""Tests for request signing — HMAC and canonical serialization."""

import os
from unittest.mock import patch

import pytest

from arcllm._signing import HmacSigner, canonical_payload, create_signer
from arcllm.exceptions import ArcLLMConfigError
from arcllm.types import Message, Tool


# ---------------------------------------------------------------------------
# HmacSigner
# ---------------------------------------------------------------------------


class TestHmacSigner:
    def test_deterministic_signature(self):
        signer = HmacSigner(key=b"test-secret")
        sig1 = signer.sign(b"hello world")
        sig2 = signer.sign(b"hello world")
        assert sig1 == sig2

    def test_different_input_different_signature(self):
        signer = HmacSigner(key=b"test-secret")
        sig1 = signer.sign(b"hello")
        sig2 = signer.sign(b"world")
        assert sig1 != sig2

    def test_different_key_different_signature(self):
        signer1 = HmacSigner(key=b"key-one")
        signer2 = HmacSigner(key=b"key-two")
        sig1 = signer1.sign(b"same payload")
        sig2 = signer2.sign(b"same payload")
        assert sig1 != sig2

    def test_signature_is_hex_string(self):
        signer = HmacSigner(key=b"test-secret")
        sig = signer.sign(b"payload")
        # SHA-256 hex digest is 64 characters
        assert len(sig) == 64
        assert all(c in "0123456789abcdef" for c in sig)


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
    def test_create_hmac_signer(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "my-secret"}):
            signer = create_signer("hmac-sha256", "TEST_SIGNING_KEY")
        assert isinstance(signer, HmacSigner)

    def test_missing_signing_key_env(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ArcLLMConfigError, match="not set"):
                create_signer("hmac-sha256", "MISSING_KEY_ENV")

    def test_ecdsa_without_cryptography(self):
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "cryptography":
                raise ImportError("mocked")
            return real_import(name, *args, **kwargs)

        with patch.dict(os.environ, {"TEST_KEY": "key-data"}):
            with patch("builtins.__import__", side_effect=mock_import):
                with pytest.raises(ArcLLMConfigError, match="arcllm\\[signing\\]"):
                    create_signer("ecdsa-p256", "TEST_KEY")

    def test_unknown_algorithm(self):
        with patch.dict(os.environ, {"TEST_KEY": "key-data"}):
            with pytest.raises(ArcLLMConfigError, match="Unsupported"):
                create_signer("rsa-2048", "TEST_KEY")
