"""Unit tests for arctrust.trust_store.

Covers:
- Happy path: write operators.toml / issuers.toml; load pubkey by DID.
- 0600 enforcement: chmod 0640 → TRUST_STORE_INSECURE_PERMS.
- Missing file: TRUST_STORE_FILE_MISSING with a clear error.
- Missing DID in file: TRUST_STORE_DID_UNKNOWN.
- Malformed base64: TRUST_STORE_BAD_KEY.
- Wrong-length key: TRUST_STORE_BAD_KEY.
- Bad TOML: TRUST_STORE_BAD_TOML.
- Wrong schema (no top-level [operators] table): TRUST_STORE_BAD_SCHEMA.
- TTL cache: edits within TTL are NOT seen; invalidate_cache forces reload.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from arctrust.trust_store import (
    TrustStoreError,
    invalidate_cache,
    load_issuer_pubkey,
    load_operator_pubkey,
    register_operator,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _flush_cache() -> None:
    """Ensure each test starts with a clean in-process cache."""
    invalidate_cache()
    yield  # type: ignore[misc]
    invalidate_cache()


@pytest.fixture
def trust_dir(tmp_path: Path) -> Path:
    """Create an empty trust dir under tmp_path."""
    d = tmp_path / "trust"
    d.mkdir()
    return d


def _write_operators(trust_dir: Path, did: str, pubkey: bytes, *, mode: int = 0o600) -> None:
    """Write operators.toml with a single DID → pubkey entry at the given mode."""
    file = trust_dir / "operators.toml"
    pub_b64 = base64.b64encode(pubkey).decode("ascii")
    file.write_text(
        f'[operators."{did}"]\npublic_key = "{pub_b64}"\nadded_at = "2026-04-18T00:00:00Z"\n',
        encoding="utf-8",
    )
    os.chmod(file, mode)


def _write_issuers(trust_dir: Path, did: str, pubkey: bytes, *, mode: int = 0o600) -> None:
    """Write issuers.toml with a single DID → pubkey entry at the given mode."""
    file = trust_dir / "issuers.toml"
    pub_b64 = base64.b64encode(pubkey).decode("ascii")
    file.write_text(
        f'[issuers."{did}"]\npublic_key = "{pub_b64}"\nadded_at = "2026-04-18T00:00:00Z"\n',
        encoding="utf-8",
    )
    os.chmod(file, mode)


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_load_operator_pubkey_roundtrip(self, trust_dir: Path) -> None:
        sk = SigningKey.generate()
        pubkey = bytes(sk.verify_key)
        did = "did:arc:org:operator/abc12345"
        _write_operators(trust_dir, did, pubkey)

        loaded = load_operator_pubkey(did, trust_dir=trust_dir)
        assert loaded == pubkey
        assert len(loaded) == 32

    def test_load_issuer_pubkey_roundtrip(self, trust_dir: Path) -> None:
        sk = SigningKey.generate()
        pubkey = bytes(sk.verify_key)
        did = "did:arc:org:trust-authority/deadbeef"
        _write_issuers(trust_dir, did, pubkey)

        loaded = load_issuer_pubkey(did, trust_dir=trust_dir)
        assert loaded == pubkey

    def test_multiple_operators_in_one_file(self, trust_dir: Path) -> None:
        sk1 = SigningKey.generate()
        sk2 = SigningKey.generate()
        pub1 = bytes(sk1.verify_key)
        pub2 = bytes(sk2.verify_key)
        did1 = "did:arc:org:operator/alice001"
        did2 = "did:arc:org:operator/bob00002"

        file = trust_dir / "operators.toml"
        file.write_text(
            f'[operators."{did1}"]\n'
            f'public_key = "{base64.b64encode(pub1).decode()}"\n'
            f'[operators."{did2}"]\n'
            f'public_key = "{base64.b64encode(pub2).decode()}"\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        assert load_operator_pubkey(did1, trust_dir=trust_dir) == pub1
        assert load_operator_pubkey(did2, trust_dir=trust_dir) == pub2


# ---------------------------------------------------------------------------
# Permission enforcement (0600 required)
# ---------------------------------------------------------------------------


class TestPermissionEnforcement:
    def test_group_readable_rejected(self, trust_dir: Path) -> None:
        sk = SigningKey.generate()
        did = "did:arc:org:operator/insecure"
        _write_operators(trust_dir, did, bytes(sk.verify_key), mode=0o640)

        with pytest.raises(TrustStoreError) as excinfo:
            load_operator_pubkey(did, trust_dir=trust_dir)
        assert excinfo.value.code == "TRUST_STORE_INSECURE_PERMS"

    def test_world_readable_rejected(self, trust_dir: Path) -> None:
        sk = SigningKey.generate()
        did = "did:arc:org:operator/insecure"
        _write_operators(trust_dir, did, bytes(sk.verify_key), mode=0o644)

        with pytest.raises(TrustStoreError) as excinfo:
            load_operator_pubkey(did, trust_dir=trust_dir)
        assert excinfo.value.code == "TRUST_STORE_INSECURE_PERMS"

    def test_owner_only_0400_accepted(self, trust_dir: Path) -> None:
        """0400 (read-only for owner, no group/other bits) is still secure."""
        sk = SigningKey.generate()
        did = "did:arc:org:operator/readonly"
        _write_operators(trust_dir, did, bytes(sk.verify_key), mode=0o400)
        # Should load without raising
        assert len(load_operator_pubkey(did, trust_dir=trust_dir)) == 32


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_missing_file(self, trust_dir: Path) -> None:
        with pytest.raises(TrustStoreError) as excinfo:
            load_operator_pubkey("did:arc:org:operator/x", trust_dir=trust_dir)
        assert excinfo.value.code == "TRUST_STORE_FILE_MISSING"

    def test_unknown_did(self, trust_dir: Path) -> None:
        sk = SigningKey.generate()
        _write_operators(trust_dir, "did:arc:org:operator/known", bytes(sk.verify_key))

        with pytest.raises(TrustStoreError) as excinfo:
            load_operator_pubkey("did:arc:org:operator/other", trust_dir=trust_dir)
        assert excinfo.value.code == "TRUST_STORE_DID_UNKNOWN"

    def test_malformed_base64(self, trust_dir: Path) -> None:
        file = trust_dir / "operators.toml"
        file.write_text(
            '[operators."did:arc:org:operator/bad"]\npublic_key = "!!!this_is_not_base64!!!"\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as excinfo:
            load_operator_pubkey("did:arc:org:operator/bad", trust_dir=trust_dir)
        assert excinfo.value.code == "TRUST_STORE_BAD_KEY"

    def test_wrong_length_key(self, trust_dir: Path) -> None:
        # 16-byte payload instead of 32
        short = base64.b64encode(b"\x00" * 16).decode("ascii")
        file = trust_dir / "operators.toml"
        file.write_text(
            f'[operators."did:arc:org:operator/short"]\npublic_key = "{short}"\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as excinfo:
            load_operator_pubkey("did:arc:org:operator/short", trust_dir=trust_dir)
        assert excinfo.value.code == "TRUST_STORE_BAD_KEY"
        assert excinfo.value.details["actual_length"] == 16

    def test_bad_toml(self, trust_dir: Path) -> None:
        file = trust_dir / "operators.toml"
        file.write_text("this is = = not valid toml [[", encoding="utf-8")
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as excinfo:
            load_operator_pubkey("did:arc:org:operator/any", trust_dir=trust_dir)
        assert excinfo.value.code == "TRUST_STORE_BAD_TOML"

    def test_missing_top_level_table(self, trust_dir: Path) -> None:
        file = trust_dir / "operators.toml"
        file.write_text(
            '[something_else."did:arc:org:x/1"]\npublic_key = "AA=="\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as excinfo:
            load_operator_pubkey("did:arc:org:x/1", trust_dir=trust_dir)
        assert excinfo.value.code == "TRUST_STORE_BAD_SCHEMA"

    def test_missing_public_key_field(self, trust_dir: Path) -> None:
        file = trust_dir / "operators.toml"
        file.write_text(
            '[operators."did:arc:org:operator/nofield"]\nadded_at = "2026-01-01"\n',
            encoding="utf-8",
        )
        os.chmod(file, 0o600)

        with pytest.raises(TrustStoreError) as excinfo:
            load_operator_pubkey("did:arc:org:operator/nofield", trust_dir=trust_dir)
        assert excinfo.value.code == "TRUST_STORE_BAD_SCHEMA"


# ---------------------------------------------------------------------------
# Cache semantics
# ---------------------------------------------------------------------------


class TestCacheSemantics:
    def test_cache_hits_without_invalidation(self, trust_dir: Path) -> None:
        sk1 = SigningKey.generate()
        sk2 = SigningKey.generate()
        did = "did:arc:org:operator/cachey"

        _write_operators(trust_dir, did, bytes(sk1.verify_key))
        first = load_operator_pubkey(did, trust_dir=trust_dir)
        assert first == bytes(sk1.verify_key)

        # Overwrite file contents — cache should still return sk1 until invalidated
        _write_operators(trust_dir, did, bytes(sk2.verify_key))
        cached = load_operator_pubkey(did, trust_dir=trust_dir)
        assert cached == bytes(sk1.verify_key), (
            "Within TTL, cache must return the original pubkey, not the new one"
        )

    def test_invalidate_cache_forces_reload(self, trust_dir: Path) -> None:
        sk1 = SigningKey.generate()
        sk2 = SigningKey.generate()
        did = "did:arc:org:operator/reload"

        _write_operators(trust_dir, did, bytes(sk1.verify_key))
        assert load_operator_pubkey(did, trust_dir=trust_dir) == bytes(sk1.verify_key)

        _write_operators(trust_dir, did, bytes(sk2.verify_key))
        invalidate_cache()
        assert load_operator_pubkey(did, trust_dir=trust_dir) == bytes(sk2.verify_key)

    def test_operators_and_issuers_cached_independently(self, trust_dir: Path) -> None:
        sk_op = SigningKey.generate()
        sk_iss = SigningKey.generate()
        op_did = "did:arc:org:operator/one"
        iss_did = "did:arc:org:trust-authority/one"

        _write_operators(trust_dir, op_did, bytes(sk_op.verify_key))
        _write_issuers(trust_dir, iss_did, bytes(sk_iss.verify_key))

        assert load_operator_pubkey(op_did, trust_dir=trust_dir) == bytes(sk_op.verify_key)
        assert load_issuer_pubkey(iss_did, trust_dir=trust_dir) == bytes(sk_iss.verify_key)


# ---------------------------------------------------------------------------
# register_operator — self-service operator registration
# ---------------------------------------------------------------------------


class TestRegisterOperator:
    def test_creates_file_with_0600_perms(self, trust_dir: Path) -> None:
        """register_operator creates operators.toml at 0600 when absent."""
        sk = SigningKey.generate()
        did = "did:arc:org:operator/new0001"

        register_operator(did, bytes(sk.verify_key), trust_dir=trust_dir)

        path = trust_dir / "operators.toml"
        assert path.exists()
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600

    def test_registered_operator_loadable(self, trust_dir: Path) -> None:
        """A registered DID is immediately readable via load_operator_pubkey."""
        sk = SigningKey.generate()
        pubkey = bytes(sk.verify_key)
        did = "did:arc:org:operator/rt000001"

        register_operator(did, pubkey, trust_dir=trust_dir)

        assert load_operator_pubkey(did, trust_dir=trust_dir) == pubkey

    def test_register_preserves_existing_operators(self, trust_dir: Path) -> None:
        """Registering a new DID does not clobber an already-registered one."""
        sk1 = SigningKey.generate()
        sk2 = SigningKey.generate()
        did1 = "did:arc:org:operator/alice001"
        did2 = "did:arc:org:operator/bob00002"

        _write_operators(trust_dir, did1, bytes(sk1.verify_key))
        register_operator(did2, bytes(sk2.verify_key), trust_dir=trust_dir)

        assert load_operator_pubkey(did1, trust_dir=trust_dir) == bytes(sk1.verify_key)
        assert load_operator_pubkey(did2, trust_dir=trust_dir) == bytes(sk2.verify_key)

    def test_register_is_idempotent_update(self, trust_dir: Path) -> None:
        """Re-registering the same DID replaces its pubkey (key rotation)."""
        sk1 = SigningKey.generate()
        sk2 = SigningKey.generate()
        did = "did:arc:org:operator/rotate1"

        register_operator(did, bytes(sk1.verify_key), trust_dir=trust_dir)
        register_operator(did, bytes(sk2.verify_key), trust_dir=trust_dir)

        assert load_operator_pubkey(did, trust_dir=trust_dir) == bytes(sk2.verify_key)

    def test_rejects_wrong_length_key(self, trust_dir: Path) -> None:
        """A non-32-byte key is rejected before any file is touched."""
        with pytest.raises(ValueError, match="32 bytes"):
            register_operator("did:arc:org:operator/bad", b"too-short", trust_dir=trust_dir)


# ---------------------------------------------------------------------------
# Default trust dir honors ARC_CONFIG_DIR (matches identity.py / GatewayConfig)
# ---------------------------------------------------------------------------


class TestDefaultTrustDirHonorsArcConfigDir:
    def test_register_and_load_use_arc_config_dir_when_no_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no explicit trust_dir, both calls land under $ARC_CONFIG_DIR/trust.

        This is what makes `arc identity init`'s self-registration visible to
        the live gateway's PairingStore under an isolated ARC_CONFIG_DIR
        deployment (e.g. the DGX setup) — both sides must resolve the same
        default directory.
        """
        monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
        sk = SigningKey.generate()
        did = "did:arc:org:operator/envdir01"

        register_operator(did, bytes(sk.verify_key))

        expected_file = tmp_path / "trust" / "operators.toml"
        assert expected_file.exists()

        invalidate_cache()
        assert load_operator_pubkey(did) == bytes(sk.verify_key)
