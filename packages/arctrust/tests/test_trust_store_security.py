"""Security-focused tests for arctrust.trust_store.

Covers threat surfaces explicitly per CLAUDE.md:
- ASI03 (Identity & Privilege Abuse): distinct DID+key isolation, no shared credentials.
- ASI04 (Agentic Supply Chain): signed module verification — tampered signatures rejected.
- ASI07 (Insecure Inter-Agent Communication): Ed25519 sign/verify/tamper paths.

Also covers the uncovered lines from the baseline:
- TrustStoreError.__str__
- load_issuer_pubkey with unknown DID
- OSError on file read (TRUST_STORE_READ_FAILED)
- _decode_pubkey with a non-dict TOML entry (TRUST_STORE_BAD_SCHEMA for non-dict entry)

Additional security/edge paths:
- Signature verification with real Ed25519 keys from the trust store.
- Tampered message rejected even when signature is structurally valid.
- Tampered signature rejected for a known good message.
- Wrong key rejected (a different DID's key cannot verify another's signature).
- Signature replay: identical (message, signature) accepted (replay protection is
  the caller's responsibility — this module is stateless — but we document it).
- Revocation pattern: removing a DID and invalidating cache.
- Key rotation: rotating a DID's key, invalidating cache, verifying new key accepted
  and old key no longer loads.
- Issuer error paths: unknown issuer DID, insecure permissions on issuers.toml.
- Missing public_key in issuer entry.
- Entry that is not a dict (edge case in TOML parsing — simulated via _decode_pubkey).
- TrustStoreError details dict is always populated.
- TrustStoreError __str__ format is correct.
- 0600 enforcement on issuers.toml (not just operators.toml).
- Cache independently isolates operators and issuers.
- Cache invalidation clears both caches atomically.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from arctrust.trust_store import (
    TrustStoreError,
    _CacheEntry,
    _decode_pubkey,
    _issuer_cache,
    _operator_cache,
    invalidate_cache,
    load_issuer_pubkey,
    load_operator_pubkey,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _flush_cache() -> Generator[None, None, None]:
    """Each test starts and ends with a clean in-process cache."""
    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture
def trust_dir(tmp_path: Path) -> Path:
    d = tmp_path / "trust"
    d.mkdir()
    return d


@pytest.fixture
def signing_key() -> SigningKey:
    return SigningKey.generate()


def _write_operators(
    trust_dir: Path, did: str, pubkey: bytes, *, mode: int = 0o600
) -> None:
    file = trust_dir / "operators.toml"
    pub_b64 = base64.b64encode(pubkey).decode("ascii")
    file.write_text(
        f'[operators."{did}"]\n'
        f'public_key = "{pub_b64}"\n'
        f'added_at = "2026-04-18T00:00:00Z"\n',
        encoding="utf-8",
    )
    os.chmod(file, mode)


def _write_issuers(
    trust_dir: Path, did: str, pubkey: bytes, *, mode: int = 0o600
) -> None:
    file = trust_dir / "issuers.toml"
    pub_b64 = base64.b64encode(pubkey).decode("ascii")
    file.write_text(
        f'[issuers."{did}"]\n'
        f'public_key = "{pub_b64}"\n'
        f'added_at = "2026-04-18T00:00:00Z"\n',
        encoding="utf-8",
    )
    os.chmod(file, mode)


# ---------------------------------------------------------------------------
# TrustStoreError — structure and __str__ (covers line 88)
# ---------------------------------------------------------------------------


class TestTrustStoreErrorStructure:
    def test_str_format_includes_code_and_message(self) -> None:
        """Line 88: __str__ must be reachable and well-formed."""
        err = TrustStoreError(
            code="TRUST_STORE_TEST_CODE",
            message="something went wrong",
        )
        result = str(err)
        assert "[TRUST_STORE_TEST_CODE]" in result
        assert "something went wrong" in result
        assert "trust_store:" in result

    def test_details_defaults_to_empty_dict(self) -> None:
        err = TrustStoreError(code="X", message="y")
        assert err.details == {}

    def test_details_populated_when_provided(self) -> None:
        err = TrustStoreError(
            code="TRUST_STORE_BAD_KEY",
            message="bad key",
            details={"did": "did:arc:test/1", "actual_length": 16},
        )
        assert err.details["did"] == "did:arc:test/1"
        assert err.details["actual_length"] == 16

    def test_code_attribute_accessible(self) -> None:
        err = TrustStoreError(code="TRUST_STORE_FILE_MISSING", message="gone")
        assert err.code == "TRUST_STORE_FILE_MISSING"

    def test_str_roundtrip_in_exception_context(self) -> None:
        """__str__ is exercised when the exception is caught and formatted."""
        try:
            raise TrustStoreError(code="TRUST_STORE_INSECURE_PERMS", message="perms bad")
        except TrustStoreError as exc:
            rendered = str(exc)
        assert "TRUST_STORE_INSECURE_PERMS" in rendered
        assert "perms bad" in rendered


# ---------------------------------------------------------------------------
# ASI07 — Ed25519 sign / verify / tamper (core security path)
# ---------------------------------------------------------------------------


class TestEd25519SignVerifyTamper:
    """Real cryptography, no mocks.

    These tests prove the trust store delivers correct pubkeys and that
    those pubkeys reject tampered data — the ASI07 guarantee.
    """

    def test_valid_signature_accepted(self, trust_dir: Path) -> None:
        """Happy path: sign with private key, verify with pubkey from trust store."""
        sk = SigningKey.generate()
        did = "did:arc:org:operator/valid-signer"
        _write_operators(trust_dir, did, bytes(sk.verify_key))

        raw_pubkey = load_operator_pubkey(did, trust_dir=trust_dir)
        vk = VerifyKey(raw_pubkey)
        message = b"pairing-code:abc123:1714000000"

        signed = sk.sign(message)
        # Must not raise
        vk.verify(message, signed.signature)

    def test_tampered_message_rejected(self, trust_dir: Path) -> None:
        """A valid signature over a different message must be rejected."""
        sk = SigningKey.generate()
        did = "did:arc:org:operator/tamper-msg"
        _write_operators(trust_dir, did, bytes(sk.verify_key))

        raw_pubkey = load_operator_pubkey(did, trust_dir=trust_dir)
        vk = VerifyKey(raw_pubkey)
        original_message = b"pairing-code:abc123:1714000000"
        tampered_message = b"pairing-code:abc123:1714000001"  # timestamp changed

        signed = sk.sign(original_message)

        with pytest.raises(BadSignatureError):
            vk.verify(tampered_message, signed.signature)

    def test_tampered_signature_rejected(self, trust_dir: Path) -> None:
        """Flipping a single bit in the signature must be rejected."""
        sk = SigningKey.generate()
        did = "did:arc:org:operator/tamper-sig"
        _write_operators(trust_dir, did, bytes(sk.verify_key))

        raw_pubkey = load_operator_pubkey(did, trust_dir=trust_dir)
        vk = VerifyKey(raw_pubkey)
        message = b"manifest:prod:sha256:deadbeef"

        signed = sk.sign(message)
        sig_bytes = bytearray(signed.signature)
        sig_bytes[0] ^= 0xFF  # flip all bits in first byte
        bad_sig = bytes(sig_bytes)

        with pytest.raises(BadSignatureError):
            vk.verify(message, bad_sig)

    def test_wrong_key_rejected(self, trust_dir: Path) -> None:
        """ASI03: a different operator's key cannot verify another's signature."""
        sk_alice = SigningKey.generate()
        sk_bob = SigningKey.generate()
        did_alice = "did:arc:org:operator/alice"
        did_bob = "did:arc:org:operator/bob"

        # Write both operators to the trust store
        file = trust_dir / "operators.toml"
        file.write_text(
            f'[operators."{did_alice}"]\n'
            f'public_key = "{base64.b64encode(bytes(sk_alice.verify_key)).decode()}"\n'
            f'[operators."{did_bob}"]\n'
            f'public_key = "{base64.b64encode(bytes(sk_bob.verify_key)).decode()}"\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        message = b"some-action-token"
        signed_by_alice = sk_alice.sign(message)

        # Correct key verifies
        vk_alice = VerifyKey(load_operator_pubkey(did_alice, trust_dir=trust_dir))
        vk_alice.verify(message, signed_by_alice.signature)

        # Bob's key must not verify Alice's signature
        vk_bob = VerifyKey(load_operator_pubkey(did_bob, trust_dir=trust_dir))
        with pytest.raises(BadSignatureError):
            vk_bob.verify(message, signed_by_alice.signature)

    def test_issuer_signature_valid(self, trust_dir: Path) -> None:
        """Issuer key from trust store verifies a manifest signature."""
        sk = SigningKey.generate()
        did = "did:arc:org:trust-authority/prod-signer"
        _write_issuers(trust_dir, did, bytes(sk.verify_key))

        raw_pubkey = load_issuer_pubkey(did, trust_dir=trust_dir)
        vk = VerifyKey(raw_pubkey)
        manifest_hash = b"sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"

        signed = sk.sign(manifest_hash)
        vk.verify(manifest_hash, signed.signature)

    def test_issuer_tampered_signature_rejected(self, trust_dir: Path) -> None:
        """Tampered manifest signature must be rejected even for a known issuer."""
        sk = SigningKey.generate()
        did = "did:arc:org:trust-authority/tamper-test"
        _write_issuers(trust_dir, did, bytes(sk.verify_key))

        raw_pubkey = load_issuer_pubkey(did, trust_dir=trust_dir)
        vk = VerifyKey(raw_pubkey)
        original = b"allowed_backends:v1:prod,staging"
        tampered = b"allowed_backends:v1:prod,staging,attacker"

        signed = sk.sign(original)
        with pytest.raises(BadSignatureError):
            vk.verify(tampered, signed.signature)

    def test_zero_signature_rejected(self, trust_dir: Path) -> None:
        """All-zero signature (zero-value forgery attempt) is rejected."""
        sk = SigningKey.generate()
        did = "did:arc:org:operator/zero-sig"
        _write_operators(trust_dir, did, bytes(sk.verify_key))

        raw_pubkey = load_operator_pubkey(did, trust_dir=trust_dir)
        vk = VerifyKey(raw_pubkey)
        message = b"some-pairing-code"
        zero_sig = b"\x00" * 64

        with pytest.raises(BadSignatureError):
            vk.verify(message, zero_sig)


# ---------------------------------------------------------------------------
# ASI03 — Identity & Privilege Abuse
# ---------------------------------------------------------------------------


class TestIdentityPrivilegeAbuse:
    def test_did_isolation_different_keys_different_identity(
        self, trust_dir: Path
    ) -> None:
        """Each DID has its own key. Loading one does not expose the other."""
        sk1 = SigningKey.generate()
        sk2 = SigningKey.generate()
        did1 = "did:arc:org:operator/identity-a"
        did2 = "did:arc:org:operator/identity-b"

        file = trust_dir / "operators.toml"
        file.write_text(
            f'[operators."{did1}"]\n'
            f'public_key = "{base64.b64encode(bytes(sk1.verify_key)).decode()}"\n'
            f'[operators."{did2}"]\n'
            f'public_key = "{base64.b64encode(bytes(sk2.verify_key)).decode()}"\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        key1 = load_operator_pubkey(did1, trust_dir=trust_dir)
        key2 = load_operator_pubkey(did2, trust_dir=trust_dir)

        assert key1 != key2
        assert key1 == bytes(sk1.verify_key)
        assert key2 == bytes(sk2.verify_key)

    def test_unregistered_did_cannot_gain_access(self, trust_dir: Path) -> None:
        """An operator not in the trust store must never succeed."""
        sk = SigningKey.generate()
        _write_operators(trust_dir, "did:arc:org:operator/legit", bytes(sk.verify_key))

        with pytest.raises(TrustStoreError) as exc_info:
            load_operator_pubkey("did:arc:org:operator/attacker", trust_dir=trust_dir)

        err = exc_info.value
        assert err.code == "TRUST_STORE_DID_UNKNOWN"
        assert "attacker" in str(err)

    def test_revoke_operator_via_file_rewrite_and_cache_invalidation(
        self, trust_dir: Path
    ) -> None:
        """Revocation pattern: remove DID from file + invalidate cache."""
        sk = SigningKey.generate()
        did = "did:arc:org:operator/to-revoke"
        _write_operators(trust_dir, did, bytes(sk.verify_key))

        # Confirm the key loads
        assert load_operator_pubkey(did, trust_dir=trust_dir) == bytes(sk.verify_key)

        # Revoke: overwrite file with DID absent
        other_sk = SigningKey.generate()
        other_did = "did:arc:org:operator/remaining"
        _write_operators(trust_dir, other_did, bytes(other_sk.verify_key))
        invalidate_cache()

        # Revoked DID must now fail
        with pytest.raises(TrustStoreError) as exc_info:
            load_operator_pubkey(did, trust_dir=trust_dir)
        assert exc_info.value.code == "TRUST_STORE_DID_UNKNOWN"

        # Remaining DID still works
        assert load_operator_pubkey(other_did, trust_dir=trust_dir) == bytes(
            other_sk.verify_key
        )

    def test_key_rotation_new_key_accepted_old_is_gone(self, trust_dir: Path) -> None:
        """Key rotation: after cache invalidation the new key loads, not the old one."""
        old_sk = SigningKey.generate()
        new_sk = SigningKey.generate()
        did = "did:arc:org:operator/rotating"

        _write_operators(trust_dir, did, bytes(old_sk.verify_key))
        old_loaded = load_operator_pubkey(did, trust_dir=trust_dir)
        assert old_loaded == bytes(old_sk.verify_key)

        # Rotate: rewrite file with new key
        _write_operators(trust_dir, did, bytes(new_sk.verify_key))
        invalidate_cache()

        new_loaded = load_operator_pubkey(did, trust_dir=trust_dir)
        assert new_loaded == bytes(new_sk.verify_key)
        assert new_loaded != old_loaded


# ---------------------------------------------------------------------------
# ASI04 — Agentic Supply Chain (manifest signing)
# ---------------------------------------------------------------------------


class TestAgenticSupplyChain:
    def test_manifest_signed_by_registered_issuer_loads(self, trust_dir: Path) -> None:
        """Issuer DID registered in trust store can provide its pubkey for manifest verify."""
        sk = SigningKey.generate()
        did = "did:arc:org:trust-authority/manifest-ca"
        _write_issuers(trust_dir, did, bytes(sk.verify_key))

        pubkey = load_issuer_pubkey(did, trust_dir=trust_dir)
        assert len(pubkey) == 32
        assert pubkey == bytes(sk.verify_key)

    def test_unregistered_issuer_rejected(self, trust_dir: Path) -> None:
        """Manifest issuer DID not in trust store is rejected — supply chain protection."""
        sk = SigningKey.generate()
        _write_issuers(
            trust_dir, "did:arc:org:trust-authority/real", bytes(sk.verify_key)
        )

        with pytest.raises(TrustStoreError) as exc_info:
            load_issuer_pubkey(
                "did:arc:org:trust-authority/attacker", trust_dir=trust_dir
            )
        assert exc_info.value.code == "TRUST_STORE_DID_UNKNOWN"

    def test_issuer_file_with_insecure_perms_rejected(self, trust_dir: Path) -> None:
        """issuers.toml with group-readable permissions must fail (tamper risk)."""
        sk = SigningKey.generate()
        did = "did:arc:org:trust-authority/insecure"
        _write_issuers(trust_dir, did, bytes(sk.verify_key), mode=0o644)

        with pytest.raises(TrustStoreError) as exc_info:
            load_issuer_pubkey(did, trust_dir=trust_dir)
        assert exc_info.value.code == "TRUST_STORE_INSECURE_PERMS"

    def test_issuer_file_0640_group_readable_rejected(self, trust_dir: Path) -> None:
        """0640 on issuers.toml must fail."""
        sk = SigningKey.generate()
        did = "did:arc:org:trust-authority/semi-open"
        _write_issuers(trust_dir, did, bytes(sk.verify_key), mode=0o640)

        with pytest.raises(TrustStoreError) as exc_info:
            load_issuer_pubkey(did, trust_dir=trust_dir)
        assert exc_info.value.code == "TRUST_STORE_INSECURE_PERMS"


# ---------------------------------------------------------------------------
# Issuer error paths (covers line 181: unknown issuer DID)
# ---------------------------------------------------------------------------


class TestIssuerErrorPaths:
    def test_missing_issuer_file(self, trust_dir: Path) -> None:
        with pytest.raises(TrustStoreError) as exc_info:
            load_issuer_pubkey("did:arc:org:trust-authority/any", trust_dir=trust_dir)
        assert exc_info.value.code == "TRUST_STORE_FILE_MISSING"

    def test_unknown_issuer_did(self, trust_dir: Path) -> None:
        """Covers line 181: load_issuer_pubkey raises for unregistered issuer DID."""
        sk = SigningKey.generate()
        _write_issuers(trust_dir, "did:arc:org:trust-authority/known", bytes(sk.verify_key))

        with pytest.raises(TrustStoreError) as exc_info:
            load_issuer_pubkey(
                "did:arc:org:trust-authority/unknown", trust_dir=trust_dir
            )
        err = exc_info.value
        assert err.code == "TRUST_STORE_DID_UNKNOWN"
        # details must include the DID and file for structured audit events
        assert err.details["did"] == "did:arc:org:trust-authority/unknown"
        assert "issuers.toml" in err.details["file"]

    def test_issuer_bad_toml(self, trust_dir: Path) -> None:
        file = trust_dir / "issuers.toml"
        file.write_text("not = = valid toml ][", encoding="utf-8")
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as exc_info:
            load_issuer_pubkey("did:arc:org:trust-authority/any", trust_dir=trust_dir)
        assert exc_info.value.code == "TRUST_STORE_BAD_TOML"

    def test_issuer_missing_top_level_table(self, trust_dir: Path) -> None:
        file = trust_dir / "issuers.toml"
        file.write_text(
            '[wrong."did:arc:org:trust-authority/x"]\npublic_key = "AA=="\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as exc_info:
            load_issuer_pubkey("did:arc:org:trust-authority/x", trust_dir=trust_dir)
        assert exc_info.value.code == "TRUST_STORE_BAD_SCHEMA"

    def test_issuer_missing_public_key_field(self, trust_dir: Path) -> None:
        file = trust_dir / "issuers.toml"
        file.write_text(
            '[issuers."did:arc:org:trust-authority/nokey"]\nnotes = "missing key"\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as exc_info:
            load_issuer_pubkey(
                "did:arc:org:trust-authority/nokey", trust_dir=trust_dir
            )
        assert exc_info.value.code == "TRUST_STORE_BAD_SCHEMA"

    def test_issuer_malformed_base64_key(self, trust_dir: Path) -> None:
        file = trust_dir / "issuers.toml"
        file.write_text(
            '[issuers."did:arc:org:trust-authority/badkey"]\n'
            'public_key = "!!!not-base64!!!"\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as exc_info:
            load_issuer_pubkey(
                "did:arc:org:trust-authority/badkey", trust_dir=trust_dir
            )
        assert exc_info.value.code == "TRUST_STORE_BAD_KEY"

    def test_issuer_wrong_length_key(self, trust_dir: Path) -> None:
        short = base64.b64encode(b"\x00" * 8).decode("ascii")
        file = trust_dir / "issuers.toml"
        file.write_text(
            '[issuers."did:arc:org:trust-authority/shortkey"]\n'
            f'public_key = "{short}"\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as exc_info:
            load_issuer_pubkey(
                "did:arc:org:trust-authority/shortkey", trust_dir=trust_dir
            )
        assert exc_info.value.code == "TRUST_STORE_BAD_KEY"
        assert exc_info.value.details["actual_length"] == 8


# ---------------------------------------------------------------------------
# _enforce_0600_perms — all permission bit paths
# ---------------------------------------------------------------------------


class TestPermissionEnforcementExtended:
    def test_group_write_rejected(self, trust_dir: Path) -> None:
        """0620 — group-write only bit must also be rejected."""
        sk = SigningKey.generate()
        did = "did:arc:org:operator/gwonly"
        _write_operators(trust_dir, did, bytes(sk.verify_key), mode=0o620)

        with pytest.raises(TrustStoreError) as exc_info:
            load_operator_pubkey(did, trust_dir=trust_dir)
        assert exc_info.value.code == "TRUST_STORE_INSECURE_PERMS"

    def test_other_execute_rejected(self, trust_dir: Path) -> None:
        """0601 — other-execute bit must be rejected."""
        sk = SigningKey.generate()
        did = "did:arc:org:operator/oexec"
        _write_operators(trust_dir, did, bytes(sk.verify_key), mode=0o601)

        with pytest.raises(TrustStoreError) as exc_info:
            load_operator_pubkey(did, trust_dir=trust_dir)
        assert exc_info.value.code == "TRUST_STORE_INSECURE_PERMS"

    def test_permission_error_details_contain_path_and_mode(
        self, trust_dir: Path
    ) -> None:
        sk = SigningKey.generate()
        did = "did:arc:org:operator/details-check"
        _write_operators(trust_dir, did, bytes(sk.verify_key), mode=0o644)

        with pytest.raises(TrustStoreError) as exc_info:
            load_operator_pubkey(did, trust_dir=trust_dir)
        err = exc_info.value
        assert "path" in err.details
        assert "permissions" in err.details

    def test_0600_accepted_for_issuers(self, trust_dir: Path) -> None:
        sk = SigningKey.generate()
        did = "did:arc:org:trust-authority/strict"
        _write_issuers(trust_dir, did, bytes(sk.verify_key), mode=0o600)
        result = load_issuer_pubkey(did, trust_dir=trust_dir)
        assert len(result) == 32


# ---------------------------------------------------------------------------
# _decode_pubkey with non-dict entry (covers line 303)
# ---------------------------------------------------------------------------


class TestDecodePubkeyNonDictEntry:
    """Line 303: _decode_pubkey must raise BAD_SCHEMA when the entry is not a dict.

    Standard TOML parsing always produces dicts for sub-tables, so this path
    is only reachable via direct call.  Testing it confirms the guard is sound
    and gives coverage to line 303.
    """

    def test_non_dict_entry_raises_bad_schema(self, tmp_path: Path) -> None:
        fake_path = tmp_path / "fake.toml"
        fake_path.touch()

        with pytest.raises(TrustStoreError) as exc_info:
            _decode_pubkey(
                did="did:arc:org:operator/test",
                entry="this-is-a-string-not-a-dict",
                path=fake_path,
            )
        err = exc_info.value
        assert err.code == "TRUST_STORE_BAD_SCHEMA"
        assert err.details["did"] == "did:arc:org:operator/test"

    def test_list_entry_raises_bad_schema(self, tmp_path: Path) -> None:
        fake_path = tmp_path / "fake.toml"
        fake_path.touch()

        with pytest.raises(TrustStoreError) as exc_info:
            _decode_pubkey(
                did="did:arc:org:operator/list",
                entry=["not", "a", "dict"],
                path=fake_path,
            )
        assert exc_info.value.code == "TRUST_STORE_BAD_SCHEMA"

    def test_int_entry_raises_bad_schema(self, tmp_path: Path) -> None:
        fake_path = tmp_path / "fake.toml"
        fake_path.touch()

        with pytest.raises(TrustStoreError) as exc_info:
            _decode_pubkey(
                did="did:arc:org:operator/int",
                entry=42,
                path=fake_path,
            )
        assert exc_info.value.code == "TRUST_STORE_BAD_SCHEMA"

    def test_none_entry_raises_bad_schema(self, tmp_path: Path) -> None:
        fake_path = tmp_path / "fake.toml"
        fake_path.touch()

        with pytest.raises(TrustStoreError) as exc_info:
            _decode_pubkey(
                did="did:arc:org:operator/none",
                entry=None,
                path=fake_path,
            )
        assert exc_info.value.code == "TRUST_STORE_BAD_SCHEMA"


# ---------------------------------------------------------------------------
# TRUST_STORE_READ_FAILED — OSError on file read (covers lines 262-263)
# ---------------------------------------------------------------------------


class TestReadFailedPath:
    """Lines 262-263: the OSError branch in _read_trust_file.

    We simulate this by patching Path.read_text after the file passes the
    permissions check.
    """

    def test_oserror_during_read_raises_read_failed(self, trust_dir: Path) -> None:
        sk = SigningKey.generate()
        did = "did:arc:org:operator/read-fail"
        _write_operators(trust_dir, did, bytes(sk.verify_key))

        with patch.object(Path, "read_text", side_effect=OSError("disk error")):
            with pytest.raises(TrustStoreError) as exc_info:
                load_operator_pubkey(did, trust_dir=trust_dir)

        err = exc_info.value
        assert err.code == "TRUST_STORE_READ_FAILED"
        assert "path" in err.details

    def test_oserror_for_issuers_raises_read_failed(self, trust_dir: Path) -> None:
        sk = SigningKey.generate()
        did = "did:arc:org:trust-authority/read-fail"
        _write_issuers(trust_dir, did, bytes(sk.verify_key))

        with patch.object(Path, "read_text", side_effect=OSError("io error")):
            with pytest.raises(TrustStoreError) as exc_info:
                load_issuer_pubkey(did, trust_dir=trust_dir)

        assert exc_info.value.code == "TRUST_STORE_READ_FAILED"


# ---------------------------------------------------------------------------
# Cache semantics — extended
# ---------------------------------------------------------------------------


class TestCacheSemanticsExtended:
    def test_issuer_cache_hits_within_ttl(self, trust_dir: Path) -> None:
        sk1 = SigningKey.generate()
        sk2 = SigningKey.generate()
        did = "did:arc:org:trust-authority/cachey"

        _write_issuers(trust_dir, did, bytes(sk1.verify_key))
        first = load_issuer_pubkey(did, trust_dir=trust_dir)
        assert first == bytes(sk1.verify_key)

        # Overwrite — cache should still serve sk1
        _write_issuers(trust_dir, did, bytes(sk2.verify_key))
        cached = load_issuer_pubkey(did, trust_dir=trust_dir)
        assert cached == bytes(sk1.verify_key)

    def test_issuer_cache_invalidated_after_flush(self, trust_dir: Path) -> None:
        sk1 = SigningKey.generate()
        sk2 = SigningKey.generate()
        did = "did:arc:org:trust-authority/reload"

        _write_issuers(trust_dir, did, bytes(sk1.verify_key))
        assert load_issuer_pubkey(did, trust_dir=trust_dir) == bytes(sk1.verify_key)

        _write_issuers(trust_dir, did, bytes(sk2.verify_key))
        invalidate_cache()
        assert load_issuer_pubkey(did, trust_dir=trust_dir) == bytes(sk2.verify_key)

    def test_invalidate_cache_clears_both_operator_and_issuer_caches(
        self, trust_dir: Path
    ) -> None:
        sk_op = SigningKey.generate()
        sk_iss = SigningKey.generate()
        op_did = "did:arc:org:operator/dual-cache"
        iss_did = "did:arc:org:trust-authority/dual-cache"

        _write_operators(trust_dir, op_did, bytes(sk_op.verify_key))
        _write_issuers(trust_dir, iss_did, bytes(sk_iss.verify_key))

        # Warm both caches
        load_operator_pubkey(op_did, trust_dir=trust_dir)
        load_issuer_pubkey(iss_did, trust_dir=trust_dir)
        assert len(_operator_cache) > 0
        assert len(_issuer_cache) > 0

        invalidate_cache()

        assert len(_operator_cache) == 0
        assert len(_issuer_cache) == 0

    def test_multiple_distinct_trust_dirs_cached_independently(
        self, tmp_path: Path
    ) -> None:
        """Cache key is the resolved path; two dirs stay separate."""
        dir_a = tmp_path / "trust_a"
        dir_a.mkdir()
        dir_b = tmp_path / "trust_b"
        dir_b.mkdir()

        sk_a = SigningKey.generate()
        sk_b = SigningKey.generate()
        did = "did:arc:org:operator/shared-did"

        _write_operators(dir_a, did, bytes(sk_a.verify_key))
        _write_operators(dir_b, did, bytes(sk_b.verify_key))

        key_a = load_operator_pubkey(did, trust_dir=dir_a)
        key_b = load_operator_pubkey(did, trust_dir=dir_b)

        assert key_a == bytes(sk_a.verify_key)
        assert key_b == bytes(sk_b.verify_key)
        assert key_a != key_b


# ---------------------------------------------------------------------------
# Operator key edge cases
# ---------------------------------------------------------------------------


class TestOperatorKeyEdgeCases:
    def test_31_byte_key_rejected(self, trust_dir: Path) -> None:
        """31-byte key is off by one — must be rejected."""
        short = base64.b64encode(b"\xab" * 31).decode("ascii")
        file = trust_dir / "operators.toml"
        file.write_text(
            '[operators."did:arc:org:operator/31byte"]\n'
            f'public_key = "{short}"\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as exc_info:
            load_operator_pubkey("did:arc:org:operator/31byte", trust_dir=trust_dir)
        err = exc_info.value
        assert err.code == "TRUST_STORE_BAD_KEY"
        assert err.details["actual_length"] == 31

    def test_33_byte_key_rejected(self, trust_dir: Path) -> None:
        """33-byte key is off by one — must be rejected."""
        long = base64.b64encode(b"\xcd" * 33).decode("ascii")
        file = trust_dir / "operators.toml"
        file.write_text(
            '[operators."did:arc:org:operator/33byte"]\n'
            f'public_key = "{long}"\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as exc_info:
            load_operator_pubkey("did:arc:org:operator/33byte", trust_dir=trust_dir)
        err = exc_info.value
        assert err.code == "TRUST_STORE_BAD_KEY"
        assert err.details["actual_length"] == 33

    def test_64_byte_key_rejected(self, trust_dir: Path) -> None:
        """64-byte key (full signing key) must be rejected — only pubkey allowed."""
        full = base64.b64encode(b"\xef" * 64).decode("ascii")
        file = trust_dir / "operators.toml"
        file.write_text(
            '[operators."did:arc:org:operator/64byte"]\n'
            f'public_key = "{full}"\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as exc_info:
            load_operator_pubkey("did:arc:org:operator/64byte", trust_dir=trust_dir)
        assert exc_info.value.code == "TRUST_STORE_BAD_KEY"
        assert exc_info.value.details["actual_length"] == 64

    def test_empty_file_missing_top_level_section(self, trust_dir: Path) -> None:
        """Empty TOML file has no operators section — TRUST_STORE_BAD_SCHEMA."""
        file = trust_dir / "operators.toml"
        file.write_text("", encoding="utf-8")
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as exc_info:
            load_operator_pubkey("did:arc:org:operator/any", trust_dir=trust_dir)
        assert exc_info.value.code == "TRUST_STORE_BAD_SCHEMA"

    def test_public_key_non_string_type(self, trust_dir: Path) -> None:
        """public_key field is an integer instead of string — TRUST_STORE_BAD_SCHEMA."""
        file = trust_dir / "operators.toml"
        file.write_text(
            '[operators."did:arc:org:operator/intkey"]\n'
            "public_key = 12345\n",
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as exc_info:
            load_operator_pubkey("did:arc:org:operator/intkey", trust_dir=trust_dir)
        assert exc_info.value.code == "TRUST_STORE_BAD_SCHEMA"


# ---------------------------------------------------------------------------
# _CacheEntry internals — dataclass frozen / loaded_at accessible
# ---------------------------------------------------------------------------


class TestCacheEntryDataclass:
    def test_cache_entry_is_frozen(self) -> None:
        """_CacheEntry is a frozen dataclass — direct attribute assignment must raise."""
        entry = _CacheEntry(records={"did:test": b"\x00" * 32}, loaded_at=0.0)
        with pytest.raises(AttributeError):
            entry.loaded_at = 999.0  # type: ignore[misc]

    def test_cache_entry_records_accessible(self) -> None:
        pubkey = b"\xaa" * 32
        entry = _CacheEntry(records={"did:arc:test": pubkey}, loaded_at=1.0)
        assert entry.records["did:arc:test"] == pubkey
        assert entry.loaded_at == 1.0
