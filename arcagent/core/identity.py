"""Identity — DID creation, Ed25519 keypair management, sign/verify.

Reuses ArcLLM's VaultResolver for secret resolution when configured,
with file-based fallback for development environments.
"""

from __future__ import annotations

import hashlib
import logging
import stat
from pathlib import Path
from typing import Any

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from arcagent.core.config import IdentityConfig
from arcagent.core.errors import IdentityError

_logger = logging.getLogger("arcagent.identity")


def _did_to_filename(did: str) -> str:
    """Convert DID to filesystem-safe filename.

    did:arc:org:type/id → did_arc_org_type_id
    """
    return did.replace(":", "_").replace("/", "_")


class AgentIdentity:
    """Ed25519 identity with DID and sign/verify capabilities.

    Supports both full (signing + verify) and verify-only modes.
    """

    def __init__(
        self,
        did: str,
        public_key: bytes,
        _signing_key: SigningKey | None = None,
    ) -> None:
        self.did = did
        self.public_key = public_key
        self._signing_key = _signing_key

    @property
    def can_sign(self) -> bool:
        """Whether this identity has a private key for signing."""
        return self._signing_key is not None

    def sign(self, message: bytes) -> bytes:
        """Sign a message with Ed25519. Returns signature bytes."""
        if self._signing_key is None:
            raise IdentityError(
                code="IDENTITY_NO_SIGNING_KEY",
                message="Cannot sign: no private key available (verify-only identity)",
            )
        signed = self._signing_key.sign(message)
        return signed.signature

    def verify(self, message: bytes, signature: bytes) -> bool:
        """Verify a signature against this identity's public key."""
        verify_key = VerifyKey(self.public_key)
        try:
            verify_key.verify(message, signature)
        except BadSignatureError:
            return False
        return True

    def save_keys(self, key_dir: Path) -> None:
        """Save keypair to filesystem with secure permissions."""
        if self._signing_key is None:
            raise IdentityError(
                code="IDENTITY_NO_SIGNING_KEY",
                message="Cannot save keys: no private key available",
            )

        key_dir = Path(key_dir)
        key_dir.mkdir(parents=True, exist_ok=True)
        key_dir.chmod(0o700)

        key_file = key_dir / f"{_did_to_filename(self.did)}.key"
        key_file.write_bytes(self._signing_key.encode())
        key_file.chmod(0o600)

        pub_file = key_dir / f"{_did_to_filename(self.did)}.pub"
        pub_file.write_bytes(self.public_key)
        pub_file.chmod(0o644)

        _logger.info("Saved keypair for %s to %s", self.did, key_dir)

    @classmethod
    def load_keys(cls, did: str, key_dir: Path) -> AgentIdentity:
        """Load keypair from filesystem with integrity verification.

        Checks that the key file exists and has secure permissions
        (0o600) before reading. Insecure permissions indicate potential
        tampering or misconfiguration.
        """
        key_dir = Path(key_dir)
        key_file = key_dir / f"{_did_to_filename(did)}.key"

        if not key_file.exists():
            raise IdentityError(
                code="IDENTITY_KEY_NOT_FOUND",
                message=f"Key file not found: {key_file}",
                details={"did": did, "key_dir": str(key_dir)},
            )

        # Verify key file permissions (must be owner-only read/write)
        file_mode = stat.S_IMODE(key_file.stat().st_mode)
        if file_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise IdentityError(
                code="IDENTITY_KEY_INSECURE",
                message=(
                    f"Key file has insecure permissions: {oct(file_mode)}. "
                    f"Expected 0o600 (owner read/write only)."
                ),
                details={
                    "did": did,
                    "key_file": str(key_file),
                    "permissions": oct(file_mode),
                },
            )

        signing_key = SigningKey(key_file.read_bytes())
        return cls(
            did=did,
            public_key=bytes(signing_key.verify_key),
            _signing_key=signing_key,
        )

    @classmethod
    def generate(cls, org: str, agent_type: str) -> AgentIdentity:
        """Generate a new Ed25519 keypair and derive DID."""
        signing_key = SigningKey.generate()
        did = cls._did_from_key(signing_key.verify_key, org, agent_type)
        return cls(
            did=did,
            public_key=bytes(signing_key.verify_key),
            _signing_key=signing_key,
        )

    @classmethod
    def from_config(
        cls,
        config: IdentityConfig,
        *,
        vault_resolver: Any = None,
        org: str = "default",
        agent_type: str = "executor",
    ) -> AgentIdentity:
        """Create identity from config with vault → file → generate fallback.

        Resolution order:
        1. Vault (if vault_resolver provided and config.vault_path set)
        2. File-based (config.key_dir / {did}.key)
        3. Generate new keypair and save to key_dir
        """
        key_dir = Path(config.key_dir).expanduser()

        # If DID is known, try loading existing keys
        if config.did:
            # Try vault first
            if vault_resolver is not None and config.vault_path:
                try:
                    return cls._load_from_vault(config.did, vault_resolver, config.vault_path)
                except Exception:
                    _logger.warning(
                        "Vault lookup failed for %s, falling back to file",
                        config.did,
                    )

            # Try file
            try:
                return cls.load_keys(config.did, key_dir)
            except IdentityError:
                _logger.warning(
                    "Key file not found for %s, will generate new",
                    config.did,
                )

        # Generate new identity
        identity = cls.generate(org=org, agent_type=agent_type)
        identity.save_keys(key_dir)
        _logger.info("Generated new identity: %s", identity.did)
        return identity

    @classmethod
    def _load_from_vault(cls, did: str, vault_resolver: Any, vault_path: str) -> AgentIdentity:
        """Load signing key from vault backend."""
        key_hex = vault_resolver.resolve_secret(vault_path, did)
        signing_key = SigningKey(bytes.fromhex(key_hex))
        return cls(
            did=did,
            public_key=bytes(signing_key.verify_key),
            _signing_key=signing_key,
        )

    @staticmethod
    def _did_from_key(verify_key: VerifyKey, org: str, agent_type: str) -> str:
        """Derive DID from public key: did:arc:{org}:{type}/{id}.

        The {id} is the first 8 hex chars of SHA-256(public_key),
        making it deterministic for the same keypair.
        """
        key_hash = hashlib.sha256(bytes(verify_key)).hexdigest()[:8]
        return f"did:arc:{org}:{agent_type}/{key_hash}"
