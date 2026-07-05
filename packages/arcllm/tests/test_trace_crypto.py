"""Tests for arcllm._trace_crypto — envelope encryption (SPEC-016)."""

import base64
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import ValidationError

from arcllm._trace_crypto import (
    assert_fips_provider_if_required,
    decode_wrapping_key,
    fips_provider_active,
    seal,
    unseal,
)
from arcllm.exceptions import ArcLLMConfigError, ArcLLMTraceIntegrityError
from arcllm.trace_store import EncryptedEnvelope


def _wrapping_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


class TestSealUnsealRoundTrip:
    def test_round_trip_equals_original(self):
        bodies = {"request_body": {"messages": [{"role": "user", "content": "hi"}]}}
        wrapping_key = _wrapping_key()

        envelope = seal(
            bodies,
            trace_id="trace-1",
            timestamp="2026-03-01T00:00:00+00:00",
            wrapping_key=wrapping_key,
            key_ref="v1",
        )
        recovered = unseal(
            envelope,
            trace_id="trace-1",
            timestamp="2026-03-01T00:00:00+00:00",
            wrapping_key=wrapping_key,
        )
        assert recovered == bodies

    def test_envelope_alg_field(self):
        wrapping_key = _wrapping_key()
        envelope = seal(
            {"a": 1}, trace_id="t", timestamp="ts", wrapping_key=wrapping_key, key_ref="v1"
        )
        assert envelope.alg == "AES-256-GCM"
        assert envelope.key_ref == "v1"

    def test_ciphertext_and_bodies_differ(self):
        """The sealed ciphertext never contains the plaintext verbatim."""
        wrapping_key = _wrapping_key()
        bodies = {"request_body": {"messages": [{"role": "user", "content": "super-secret"}]}}
        envelope = seal(
            bodies, trace_id="t", timestamp="ts", wrapping_key=wrapping_key, key_ref="v1"
        )
        assert "super-secret" not in envelope.ciphertext


class TestAadBinding:
    def test_altered_trace_id_fails_decryption(self):
        wrapping_key = _wrapping_key()
        envelope = seal(
            {"a": 1},
            trace_id="trace-1",
            timestamp="2026-03-01T00:00:00+00:00",
            wrapping_key=wrapping_key,
            key_ref="v1",
        )
        with pytest.raises(ArcLLMTraceIntegrityError, match="AAD mismatch"):
            unseal(
                envelope,
                trace_id="trace-EVIL",
                timestamp="2026-03-01T00:00:00+00:00",
                wrapping_key=wrapping_key,
            )

    def test_altered_timestamp_fails_decryption(self):
        wrapping_key = _wrapping_key()
        envelope = seal(
            {"a": 1},
            trace_id="trace-1",
            timestamp="2026-03-01T00:00:00+00:00",
            wrapping_key=wrapping_key,
            key_ref="v1",
        )
        with pytest.raises(ArcLLMTraceIntegrityError):
            unseal(
                envelope,
                trace_id="trace-1",
                timestamp="2099-01-01T00:00:00+00:00",
                wrapping_key=wrapping_key,
            )

    def test_wrong_wrapping_key_fails_authentication(self):
        """A structurally-valid AAD but wrong KEK still fails (AEAD tag check)."""
        envelope = seal(
            {"a": 1},
            trace_id="trace-1",
            timestamp="ts",
            wrapping_key=_wrapping_key(),
            key_ref="v1",
        )
        with pytest.raises(ArcLLMConfigError, match="failed to authenticate"):
            unseal(envelope, trace_id="trace-1", timestamp="ts", wrapping_key=_wrapping_key())


class TestNonceUniqueness:
    def test_nonces_unique_across_many_seals(self):
        """SC-13 — per-record DEK + unique nonce; assert both properties hold."""
        wrapping_key = _wrapping_key()
        nonces = set()
        for i in range(500):
            envelope = seal(
                {"i": i},
                trace_id=f"trace-{i}",
                timestamp="ts",
                wrapping_key=wrapping_key,
                key_ref="v1",
            )
            nonce_bytes = base64.b64decode(envelope.nonce)
            assert len(nonce_bytes) == 12  # 96-bit GCM nonce
            nonces.add(envelope.nonce)
        assert len(nonces) == 500

    def test_wrapped_keys_unique_across_seals(self):
        """Each record gets its own fresh DEK, so wrapped_key also varies."""
        wrapping_key = _wrapping_key()
        wrapped_keys = {
            seal(
                {"i": i}, trace_id=f"t{i}", timestamp="ts", wrapping_key=wrapping_key, key_ref="v1"
            ).wrapped_key
            for i in range(50)
        }
        assert len(wrapped_keys) == 50


class TestKekRotation:
    def test_both_key_refs_unseal_via_their_own_stored_key_ref(self):
        """seal under v1, rotate to v2, seal again — both still unseal (SC-12/SC-28)."""
        kek_v1 = _wrapping_key()
        kek_v2 = _wrapping_key()

        envelope_v1 = seal(
            {"body": "old"}, trace_id="t1", timestamp="ts1", wrapping_key=kek_v1, key_ref="v1"
        )
        envelope_v2 = seal(
            {"body": "new"}, trace_id="t2", timestamp="ts2", wrapping_key=kek_v2, key_ref="v2"
        )

        def resolve(key_ref: str) -> bytes:
            return {"v1": kek_v1, "v2": kek_v2}[key_ref]

        recovered_v1 = unseal(
            envelope_v1, trace_id="t1", timestamp="ts1", wrapping_key=resolve(envelope_v1.key_ref)
        )
        recovered_v2 = unseal(
            envelope_v2, trace_id="t2", timestamp="ts2", wrapping_key=resolve(envelope_v2.key_ref)
        )

        assert recovered_v1 == {"body": "old"}
        assert recovered_v2 == {"body": "new"}
        assert envelope_v1.key_ref == "v1"
        assert envelope_v2.key_ref == "v2"


class TestMissingExtra:
    def test_seal_raises_clear_error_when_cryptography_missing(self):
        with patch("arcllm._trace_crypto._import_crypto_primitives") as mock_import:
            mock_import.side_effect = ArcLLMConfigError(
                "encryption enabled but arcllm[trace-encryption] not installed "
                "(pip install arcllm[trace-encryption])"
            )
            with pytest.raises(ArcLLMConfigError, match="arcllm\\[trace-encryption\\]"):
                seal({"a": 1}, trace_id="t", timestamp="ts", wrapping_key=b"x" * 32, key_ref="v1")

    def test_import_crypto_primitives_raises_when_module_actually_absent(self):
        """Exercises the real ImportError branch (not the mocked-out one above)."""
        import sys

        from arcllm._trace_crypto import _import_crypto_primitives

        with patch.dict(sys.modules, {"cryptography.hazmat.primitives.keywrap": None}):
            with pytest.raises(ArcLLMConfigError, match="arcllm\\[trace-encryption\\]"):
                _import_crypto_primitives()

    def test_fips_provider_active_false_when_backend_module_absent(self):
        """Exercises the real ImportError branch inside fips_provider_active()."""
        import sys

        with patch.dict(sys.modules, {"cryptography.hazmat.backends.openssl.backend": None}):
            assert fips_provider_active() is False


class TestDecodeWrappingKey:
    def test_valid_base64_32_bytes(self):
        raw = b"x" * 32
        secret = base64.b64encode(raw).decode("ascii")
        assert decode_wrapping_key(secret) == raw

    def test_invalid_base64_raises(self):
        with pytest.raises(ArcLLMConfigError, match="base64"):
            decode_wrapping_key("not-valid-base64!!!")

    def test_wrong_length_raises(self):
        secret = base64.b64encode(b"x" * 16).decode("ascii")
        with pytest.raises(ArcLLMConfigError, match="32 bytes"):
            decode_wrapping_key(secret)


class TestFipsSelfCheck:
    def test_require_fips_false_never_checks(self):
        """Personal/enterprise tiers (require_fips=False) never gate on FIPS."""
        assert_fips_provider_if_required(require_fips=False)  # must not raise

    def test_require_fips_true_with_non_fips_provider_fails_closed(self):
        with patch("arcllm._trace_crypto.fips_provider_active", return_value=False):
            with pytest.raises(ArcLLMConfigError, match="FIPS-140-3-approved"):
                assert_fips_provider_if_required(require_fips=True)

    def test_require_fips_true_with_fips_provider_passes(self):
        with patch("arcllm._trace_crypto.fips_provider_active", return_value=True):
            assert_fips_provider_if_required(require_fips=True)  # must not raise

    def test_fips_provider_active_returns_bool(self):
        """Dev environment's vendored OpenSSL is not FIPS-validated."""
        assert fips_provider_active() is False


class TestEncryptedEnvelopeSchema:
    def test_envelope_is_frozen(self):
        envelope = EncryptedEnvelope(
            wrapped_key="a", key_ref="v1", nonce="b", ciphertext="c", aad="d"
        )
        with pytest.raises(ValidationError):
            envelope.key_ref = "v2"  # type: ignore[misc]
