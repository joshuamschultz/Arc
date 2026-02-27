"""Tests for identity — DID, Ed25519 keypair, sign/verify, file storage."""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from nacl.signing import SigningKey

from arcagent.core.config import IdentityConfig
from arcagent.core.errors import IdentityError
from arcagent.core.identity import AgentIdentity


class TestGenerate:
    def test_generates_valid_identity(self) -> None:
        identity = AgentIdentity.generate(org="blackarc", agent_type="executor")
        assert identity.did.startswith("did:arc:blackarc:executor/")
        assert len(identity.public_key) == 32
        assert identity.can_sign

    def test_did_format(self) -> None:
        identity = AgentIdentity.generate(org="acme", agent_type="planner")
        parts = identity.did.split(":")
        assert parts[0] == "did"
        assert parts[1] == "arc"
        assert parts[2] == "acme"
        # type/id portion
        type_id = parts[3]
        assert type_id.startswith("planner/")
        # id is 8 hex chars
        hex_id = type_id.split("/")[1]
        assert len(hex_id) == 8
        int(hex_id, 16)  # Must be valid hex

    def test_deterministic_did_from_same_key(self) -> None:
        """Same keypair always produces the same DID."""
        key = SigningKey.generate()
        id1 = AgentIdentity._did_from_key(key.verify_key, "org", "type")
        id2 = AgentIdentity._did_from_key(key.verify_key, "org", "type")
        assert id1 == id2


class TestSignVerify:
    def test_sign_and_verify_roundtrip(self) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        message = b"hello world"
        signature = identity.sign(message)
        assert identity.verify(message, signature)

    def test_verify_wrong_message_fails(self) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        signature = identity.sign(b"hello")
        assert not identity.verify(b"wrong message", signature)

    def test_verify_wrong_key_fails(self) -> None:
        id1 = AgentIdentity.generate(org="test", agent_type="executor")
        id2 = AgentIdentity.generate(org="test", agent_type="executor")
        signature = id1.sign(b"hello")
        assert not id2.verify(b"hello", signature)

    def test_sign_without_signing_key_raises(self) -> None:
        """Verify-only identity cannot sign."""
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        verify_only = AgentIdentity(
            did=identity.did,
            public_key=identity.public_key,
            _signing_key=None,
        )
        with pytest.raises(IdentityError) as exc_info:
            verify_only.sign(b"hello")
        assert exc_info.value.code == "IDENTITY_NO_SIGNING_KEY"


class TestFileStorage:
    def test_save_and_load_keypair(self, tmp_path: Path) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        identity.save_keys(tmp_path)

        loaded = AgentIdentity.load_keys(identity.did, tmp_path)
        assert loaded.did == identity.did
        assert loaded.public_key == identity.public_key
        assert loaded.can_sign

        # Roundtrip verify
        sig = loaded.sign(b"test")
        assert identity.verify(b"test", sig)

    def test_save_keys_without_signing_key_raises(self, tmp_path: Path) -> None:
        """Verify-only identity cannot save keys."""
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        verify_only = AgentIdentity(
            did=identity.did,
            public_key=identity.public_key,
            _signing_key=None,
        )
        with pytest.raises(IdentityError) as exc_info:
            verify_only.save_keys(tmp_path)
        assert exc_info.value.code == "IDENTITY_NO_SIGNING_KEY"

    def test_key_file_permissions(self, tmp_path: Path) -> None:
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        identity.save_keys(tmp_path)

        safe_name = identity.did.replace(":", "_").replace("/", "_")
        key_file = tmp_path / f"{safe_name}.key"
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600

    def test_key_dir_permissions(self, tmp_path: Path) -> None:
        key_dir = tmp_path / "keys"
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        identity.save_keys(key_dir)

        mode = stat.S_IMODE(key_dir.stat().st_mode)
        assert mode == 0o700

    def test_load_nonexistent_raises(self, tmp_path: Path) -> None:
        with pytest.raises(IdentityError) as exc_info:
            AgentIdentity.load_keys("did:arc:test:executor/nonexist", tmp_path)
        assert exc_info.value.code == "IDENTITY_KEY_NOT_FOUND"


class TestKeyFileIntegrity:
    def test_insecure_key_permissions_rejected(self, tmp_path: Path) -> None:
        """Key files with group/other permissions are rejected."""
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        identity.save_keys(tmp_path)

        safe_name = identity.did.replace(":", "_").replace("/", "_")
        key_file = tmp_path / f"{safe_name}.key"
        # Make group-readable (insecure)
        key_file.chmod(0o640)

        with pytest.raises(IdentityError) as exc_info:
            AgentIdentity.load_keys(identity.did, tmp_path)
        assert exc_info.value.code == "IDENTITY_KEY_INSECURE"

    def test_world_readable_key_rejected(self, tmp_path: Path) -> None:
        """Key files readable by others are rejected."""
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        identity.save_keys(tmp_path)

        safe_name = identity.did.replace(":", "_").replace("/", "_")
        key_file = tmp_path / f"{safe_name}.key"
        key_file.chmod(0o644)

        with pytest.raises(IdentityError) as exc_info:
            AgentIdentity.load_keys(identity.did, tmp_path)
        assert exc_info.value.code == "IDENTITY_KEY_INSECURE"

    def test_secure_permissions_accepted(self, tmp_path: Path) -> None:
        """Key files with 0o600 are accepted."""
        identity = AgentIdentity.generate(org="test", agent_type="executor")
        identity.save_keys(tmp_path)
        # save_keys sets 0o600 by default
        loaded = AgentIdentity.load_keys(identity.did, tmp_path)
        assert loaded.did == identity.did


class TestFromConfig:
    def test_auto_generate_when_no_keys_exist(self, tmp_path: Path) -> None:
        config = IdentityConfig(
            did="",
            key_dir=str(tmp_path / "keys"),
            vault_path="",
        )
        identity = AgentIdentity.from_config(config)
        assert identity.did.startswith("did:arc:default:executor/")
        assert identity.can_sign
        # Keys were saved
        key_dir = Path(config.key_dir)
        assert key_dir.exists()

    def test_load_existing_keys(self, tmp_path: Path) -> None:
        # First generate and save
        original = AgentIdentity.generate(org="test", agent_type="planner")
        key_dir = tmp_path / "keys"
        original.save_keys(key_dir)

        config = IdentityConfig(
            did=original.did,
            key_dir=str(key_dir),
            vault_path="",
        )
        loaded = AgentIdentity.from_config(config)
        assert loaded.did == original.did
        assert loaded.public_key == original.public_key

    def test_from_config_with_org_and_type(self, tmp_path: Path) -> None:
        """from_config uses defaults when DID is empty."""
        config = IdentityConfig(
            did="",
            key_dir=str(tmp_path / "keys"),
            vault_path="",
        )
        identity = AgentIdentity.from_config(config, org="acme", agent_type="reviewer")
        assert "acme" in identity.did
        assert "reviewer" in identity.did


class TestVaultFallback:
    def test_vault_hit_returns_identity(self, tmp_path: Path) -> None:
        """When vault has the key, use it."""
        original = AgentIdentity.generate(org="test", agent_type="executor")

        mock_resolver = MagicMock()
        mock_resolver.resolve_secret.return_value = original._signing_key.encode().hex()

        config = IdentityConfig(
            did=original.did,
            key_dir=str(tmp_path / "keys"),
            vault_path="secret/agents/test",
        )
        identity = AgentIdentity.from_config(config, vault_resolver=mock_resolver)
        assert identity.did == original.did
        assert identity.can_sign

    def test_vault_miss_falls_back_to_file(self, tmp_path: Path) -> None:
        """When vault fails, fall back to file-based keys."""
        original = AgentIdentity.generate(org="test", agent_type="executor")
        key_dir = tmp_path / "keys"
        original.save_keys(key_dir)

        mock_resolver = MagicMock()
        mock_resolver.resolve_secret.side_effect = Exception("vault unavailable")

        config = IdentityConfig(
            did=original.did,
            key_dir=str(key_dir),
            vault_path="secret/agents/test",
        )
        identity = AgentIdentity.from_config(config, vault_resolver=mock_resolver)
        assert identity.did == original.did
        assert identity.can_sign

    def test_no_vault_uses_file(self, tmp_path: Path) -> None:
        """When no vault resolver provided, use file-based keys."""
        original = AgentIdentity.generate(org="test", agent_type="executor")
        key_dir = tmp_path / "keys"
        original.save_keys(key_dir)

        config = IdentityConfig(
            did=original.did,
            key_dir=str(key_dir),
            vault_path="",
        )
        identity = AgentIdentity.from_config(config, vault_resolver=None)
        assert identity.did == original.did
        assert identity.can_sign
