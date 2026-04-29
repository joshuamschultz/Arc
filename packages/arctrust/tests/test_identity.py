"""Tests for arctrust.identity — DID, Ed25519 keypair, sign/verify, HKDF child derivation."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from arctrust.identity import (
    AgentIdentity,
    ChildIdentity,
    derive_child_identity,
    generate_did,
    parse_did,
    validate_did,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeIdentityConfig:
    """Minimal stand-in for IdentityConfig so tests don't depend on arcagent."""

    def __init__(
        self,
        did: str = "",
        key_dir: str = "/tmp/keys",
        vault_path: str = "",
    ) -> None:
        self.did = did
        self.key_dir = key_dir
        self.vault_path = vault_path


# ---------------------------------------------------------------------------
# DID utilities
# ---------------------------------------------------------------------------


class TestGenerateDid:
    def test_format_matches_spec(self) -> None:
        """DID must be did:arc:{org}:{type}/{hash} where hash is 8 hex chars."""
        key = SigningKey.generate()
        did = generate_did(key.verify_key, org="test", agent_type="executor")
        parts = did.split(":")
        assert parts[0] == "did"
        assert parts[1] == "arc"
        assert parts[2] == "test"
        type_id = parts[3]
        assert "/" in type_id
        hex_id = type_id.split("/")[1]
        assert len(hex_id) == 8
        int(hex_id, 16)  # must be valid hex

    def test_deterministic_for_same_key(self) -> None:
        key = SigningKey.generate()
        d1 = generate_did(key.verify_key, org="org", agent_type="type")
        d2 = generate_did(key.verify_key, org="org", agent_type="type")
        assert d1 == d2

    def test_different_key_different_did(self) -> None:
        k1 = SigningKey.generate()
        k2 = SigningKey.generate()
        assert generate_did(k1.verify_key, org="o", agent_type="t") != generate_did(
            k2.verify_key, org="o", agent_type="t"
        )


class TestParseDid:
    def test_valid_did_returns_components(self) -> None:
        did = "did:arc:myorg:executor/abcd1234"
        result = parse_did(did)
        assert result["org"] == "myorg"
        assert result["agent_type"] == "executor"
        assert result["hash"] == "abcd1234"

    def test_invalid_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid DID"):
            parse_did("did:other:something/hash")

    def test_missing_hash_raises(self) -> None:
        with pytest.raises(ValueError, match="Malformed DID"):
            parse_did("did:arc:org:executor")

    def test_wrong_segment_count_raises(self) -> None:
        with pytest.raises(ValueError, match="Malformed DID"):
            parse_did("did:arc:only_three")


class TestValidateDid:
    def test_empty_string_is_valid_for_auto_generate(self) -> None:
        assert validate_did("") == ""

    def test_full_valid_did_passes(self) -> None:
        did = "did:arc:acme:planner/9b43ee77"
        assert validate_did(did) == did

    def test_non_arc_did_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid DID"):
            validate_did("did:web:example.com")

    def test_partial_hash_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid DID"):
            validate_did("9b43ee77")

    def test_malformed_structure_raises(self) -> None:
        with pytest.raises(ValueError, match="Malformed DID"):
            validate_did("did:arc:org:executor")


# ---------------------------------------------------------------------------
# AgentIdentity
# ---------------------------------------------------------------------------


class TestAgentIdentityGenerate:
    def test_generates_with_correct_did_prefix(self) -> None:
        identity = AgentIdentity.generate(org="blackarc", agent_type="executor")
        assert identity.did.startswith("did:arc:blackarc:executor/")
        assert len(identity.public_key) == 32
        assert identity.can_sign

    def test_different_orgs_different_dids(self) -> None:
        i1 = AgentIdentity.generate(org="org1", agent_type="exec")
        i2 = AgentIdentity.generate(org="org2", agent_type="exec")
        assert i1.did != i2.did


class TestAgentIdentitySignVerify:
    def test_sign_and_verify_roundtrip(self) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        message = b"hello world"
        signature = identity.sign(message)
        assert identity.verify(message, signature)

    def test_verify_wrong_message_fails(self) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        sig = identity.sign(b"hello")
        assert not identity.verify(b"wrong", sig)

    def test_verify_wrong_key_fails(self) -> None:
        id1 = AgentIdentity.generate(org="test", agent_type="executor")
        id2 = AgentIdentity.generate(org="test", agent_type="executor")
        sig = id1.sign(b"hello")
        assert not id2.verify(b"hello", sig)

    def test_sign_without_private_key_raises(self) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        verify_only = AgentIdentity(
            did=identity.did,
            public_key=identity.public_key,
            _signing_key=None,
        )
        with pytest.raises(ValueError, match="no private key"):
            verify_only.sign(b"hello")

    def test_can_sign_false_when_no_signing_key(self) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        verify_only = AgentIdentity(
            did=identity.did, public_key=identity.public_key, _signing_key=None
        )
        assert not verify_only.can_sign


class TestAgentIdentityFileStorage:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        identity.save_keys(tmp_path)
        loaded = AgentIdentity.load_keys(identity.did, tmp_path)
        assert loaded.did == identity.did
        assert loaded.public_key == identity.public_key
        assert loaded.can_sign
        # Cross-verify
        sig = loaded.sign(b"test")
        assert identity.verify(b"test", sig)

    def test_key_file_has_0600_permissions(self, tmp_path: Path) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        identity.save_keys(tmp_path)
        safe = identity.did.replace(":", "_").replace("/", "_")
        key_file = tmp_path / f"{safe}.key"
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600

    def test_key_dir_has_0700_permissions(self, tmp_path: Path) -> None:
        key_dir = tmp_path / "keys"
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        identity.save_keys(key_dir)
        mode = stat.S_IMODE(key_dir.stat().st_mode)
        assert mode == 0o700

    def test_load_nonexistent_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Key file not found"):
            AgentIdentity.load_keys("did:arc:test:executor/nonexist", tmp_path)

    def test_insecure_permissions_rejected(self, tmp_path: Path) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        identity.save_keys(tmp_path)
        safe = identity.did.replace(":", "_").replace("/", "_")
        key_file = tmp_path / f"{safe}.key"
        key_file.chmod(0o640)
        with pytest.raises(ValueError, match="insecure permissions"):
            AgentIdentity.load_keys(identity.did, tmp_path)

    def test_world_readable_rejected(self, tmp_path: Path) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        identity.save_keys(tmp_path)
        safe = identity.did.replace(":", "_").replace("/", "_")
        key_file = tmp_path / f"{safe}.key"
        key_file.chmod(0o644)
        with pytest.raises(ValueError, match="insecure permissions"):
            AgentIdentity.load_keys(identity.did, tmp_path)

    def test_save_without_signing_key_raises(self, tmp_path: Path) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        verify_only = AgentIdentity(
            did=identity.did, public_key=identity.public_key, _signing_key=None
        )
        with pytest.raises(ValueError, match="no private key"):
            verify_only.save_keys(tmp_path)


# ---------------------------------------------------------------------------
# ChildIdentity + derive_child_identity
# ---------------------------------------------------------------------------


class TestFromConfig:
    def test_auto_generate_when_did_empty(self, tmp_path: Path) -> None:
        config = FakeIdentityConfig(did="", key_dir=str(tmp_path / "keys"))
        identity = AgentIdentity.from_config(config)
        assert identity.did.startswith("did:arc:default:executor/")
        assert identity.can_sign

    def test_load_existing_keys(self, tmp_path: Path) -> None:
        original = AgentIdentity.generate(org="test", agent_type="planner")
        key_dir = tmp_path / "keys"
        original.save_keys(key_dir)
        config = FakeIdentityConfig(did=original.did, key_dir=str(key_dir))
        loaded = AgentIdentity.from_config(config)
        assert loaded.did == original.did
        assert loaded.can_sign

    def test_did_set_but_key_missing_raises(self, tmp_path: Path) -> None:
        config = FakeIdentityConfig(
            did="did:arc:test:executor/deadbeef",
            key_dir=str(tmp_path / "empty_keys"),
        )
        with pytest.raises(ValueError, match="Key file not found"):
            AgentIdentity.from_config(config)

    def test_writes_did_back_to_config_file(self, tmp_path: Path) -> None:
        config_file = tmp_path / "arcagent.toml"
        config_file.write_text(
            '[identity]\ndid = ""\nkey_dir = "' + str(tmp_path / "keys") + '"\n',
            encoding="utf-8",
        )
        config = FakeIdentityConfig(did="", key_dir=str(tmp_path / "keys"))
        identity = AgentIdentity.from_config(config, config_path=config_file)
        content = config_file.read_text(encoding="utf-8")
        assert identity.did in content
        assert 'did = ""' not in content

    def test_config_path_none_skips_write(self, tmp_path: Path) -> None:
        """When config_path is None, no write is attempted."""
        config = FakeIdentityConfig(did="", key_dir=str(tmp_path / "keys"))
        # Should not raise even though there's no config file
        identity = AgentIdentity.from_config(config, config_path=None)
        assert identity.did.startswith("did:arc:")

    def test_unreadable_config_path_logs_warning(self, tmp_path: Path) -> None:
        """_write_did_to_config with missing file logs warning, does not raise."""
        config = FakeIdentityConfig(did="", key_dir=str(tmp_path / "keys"))
        nonexistent = tmp_path / "nonexistent.toml"
        # Should not raise
        identity = AgentIdentity.from_config(config, config_path=nonexistent)
        assert identity.did.startswith("did:arc:")

    def test_config_without_empty_did_field_logs_warning(self, tmp_path: Path) -> None:
        """Config without a did = '' line logs warning, identity still generated."""
        config_file = tmp_path / "arcagent.toml"
        config_file.write_text(
            '[identity]\nkey_dir = "' + str(tmp_path / "keys") + '"\n',
            encoding="utf-8",
        )
        config = FakeIdentityConfig(did="", key_dir=str(tmp_path / "keys"))
        identity = AgentIdentity.from_config(config, config_path=config_file)
        assert identity.did.startswith("did:arc:")

    def test_vault_hit_returns_identity(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        original = AgentIdentity.generate(org="test", agent_type="executor")
        mock_resolver = MagicMock()
        assert original._signing_key is not None
        mock_resolver.resolve_secret.return_value = original._signing_key.encode().hex()
        config = FakeIdentityConfig(
            did=original.did,
            key_dir=str(tmp_path / "keys"),
            vault_path="secret/agents/test",
        )
        identity = AgentIdentity.from_config(config, vault_resolver=mock_resolver)
        assert identity.did == original.did
        assert identity.can_sign

    def test_vault_miss_falls_back_to_file(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        original = AgentIdentity.generate(org="test", agent_type="executor")
        key_dir = tmp_path / "keys"
        original.save_keys(key_dir)
        mock_resolver = MagicMock()
        mock_resolver.resolve_secret.side_effect = Exception("vault unavailable")
        config = FakeIdentityConfig(
            did=original.did,
            key_dir=str(key_dir),
            vault_path="secret/agents/test",
        )
        identity = AgentIdentity.from_config(config, vault_resolver=mock_resolver)
        assert identity.did == original.did
        assert identity.can_sign


class TestChildIdentity:
    def test_derive_produces_valid_did(self) -> None:
        parent_sk = SigningKey.generate().encode()
        child = derive_child_identity(parent_sk_bytes=parent_sk, spawn_id="spawn-001")
        assert child.did.startswith("did:arc:delegate:child/")
        assert len(child.sk_bytes) == 32

    def test_deterministic_for_same_inputs(self) -> None:
        parent_sk = SigningKey.generate().encode()
        c1 = derive_child_identity(parent_sk_bytes=parent_sk, spawn_id="nonce-abc")
        c2 = derive_child_identity(parent_sk_bytes=parent_sk, spawn_id="nonce-abc")
        assert c1.did == c2.did
        assert c1.sk_bytes == c2.sk_bytes

    def test_different_nonce_produces_different_child(self) -> None:
        parent_sk = SigningKey.generate().encode()
        c1 = derive_child_identity(parent_sk_bytes=parent_sk, spawn_id="nonce-1")
        c2 = derive_child_identity(parent_sk_bytes=parent_sk, spawn_id="nonce-2")
        assert c1.did != c2.did
        assert c1.sk_bytes != c2.sk_bytes

    def test_different_parent_different_child(self) -> None:
        sk1 = SigningKey.generate().encode()
        sk2 = SigningKey.generate().encode()
        c1 = derive_child_identity(parent_sk_bytes=sk1, spawn_id="same-nonce")
        c2 = derive_child_identity(parent_sk_bytes=sk2, spawn_id="same-nonce")
        assert c1.did != c2.did

    def test_positional_api_still_works(self) -> None:
        """Legacy positional call style must still work for arcrun compatibility."""
        parent_sk = SigningKey.generate().encode()
        child = derive_child_identity(parent_sk, "my-nonce", 300)
        assert child.did.startswith("did:arc:delegate:child/")
        assert child.ttl_s == 300

    def test_ttl_default(self) -> None:
        parent_sk = SigningKey.generate().encode()
        child = derive_child_identity(parent_sk_bytes=parent_sk, spawn_id="x")
        assert child.ttl_s == 300  # default

    def test_child_identity_model_fields(self) -> None:
        parent_sk = SigningKey.generate().encode()
        child = derive_child_identity(
            parent_sk_bytes=parent_sk, spawn_id="x", wallclock_timeout_s=60
        )
        assert isinstance(child, ChildIdentity)
        assert child.ttl_s == 60
        assert len(child.sk_bytes) == 32

    def test_child_sk_is_valid_signing_key(self) -> None:
        """Derived child seed must be a valid Ed25519 signing key."""
        parent_sk = SigningKey.generate().encode()
        child = derive_child_identity(parent_sk_bytes=parent_sk, spawn_id="test")
        # Should not raise
        child_signing_key = SigningKey(child.sk_bytes)
        assert len(bytes(child_signing_key.verify_key)) == 32
