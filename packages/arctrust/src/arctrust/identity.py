"""Identity — DID creation, Ed25519 keypair management, child derivation.

This module is the canonical identity primitive for all Arc packages.
Both arcagent and arcrun import from here; no Arc package implements its
own identity logic.

DID format: ``did:arc:{org}:{type}/{hash}``
  - org: deployment organization (e.g. ``default``, ``doe``)
  - type: agent role (e.g. ``executor``, ``planner``, ``child``)
  - hash: first 8 hex chars of SHA-256(public_key) — deterministic

Child identity: derived via HKDF-SHA256 from the parent secret key and a
per-spawn nonce. Children have short-lived identities that cannot be reused
across spawns.

Security:
- ASI-03: no shared credentials — each agent has a unique DID + keypair
- ASI-07: Ed25519 signing; child keys are unpredictable to observers
- Key files must have 0600 permissions (owner only); insecure perms rejected
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import stat
from pathlib import Path
from typing import Any

from nacl.signing import SigningKey, VerifyKey
from pydantic import BaseModel

from arctrust.keypair import KEY_SIZE, generate_keypair

_logger = logging.getLogger("arctrust.identity")

# ---------------------------------------------------------------------------
# DID utilities
# ---------------------------------------------------------------------------


def generate_did(verify_key: VerifyKey, *, org: str, agent_type: str) -> str:
    """Derive a DID from an Ed25519 verify key.

    The hash suffix is the first 8 hex chars of SHA-256(public_key),
    making it deterministic for the same keypair.

    Args:
        verify_key: Ed25519 verify key (PyNaCl VerifyKey).
        org: Deployment organization (e.g. ``default``).
        agent_type: Agent role (e.g. ``executor``).

    Returns:
        DID string in the form ``did:arc:{org}:{type}/{hash}``.
    """
    key_hash = hashlib.sha256(bytes(verify_key)).hexdigest()[:8]
    return f"did:arc:{org}:{agent_type}/{key_hash}"


def parse_did(did: str) -> dict[str, str]:
    """Parse a DID string into its component parts.

    Args:
        did: Full DID in ``did:arc:{org}:{type}/{hash}`` format.

    Returns:
        Dict with keys ``org``, ``agent_type``, ``hash``.

    Raises:
        ValueError: DID is not a valid ``did:arc:`` identifier.
    """
    if not did.startswith("did:arc:"):
        raise ValueError(
            f"Invalid DID: expected 'did:arc:...' prefix, got {did!r}"
        )
    parts = did.split(":")
    if len(parts) != 4 or "/" not in parts[3]:
        raise ValueError(
            f"Malformed DID structure: {did!r}. "
            "Expected 'did:arc:{org}:{type}/{hash}'."
        )
    type_hash = parts[3].split("/", 1)
    return {
        "org": parts[2],
        "agent_type": type_hash[0],
        "hash": type_hash[1],
    }


def validate_did(did: str) -> str:
    """Validate a DID string; return it if valid or empty string if blank.

    An empty string is valid — it signals auto-generation. A non-empty
    string must be a well-formed ``did:arc:`` DID.

    Args:
        did: DID to validate (or empty string for auto-generation).

    Returns:
        The original DID string if valid, or empty string if blank.

    Raises:
        ValueError: DID is non-empty but malformed.
    """
    if not did:
        return ""
    if not did.startswith("did:arc:"):
        raise ValueError(
            f"Invalid DID format: {did!r}. "
            "Must be 'did:arc:{org}:{type}/{hash}' or empty for auto-generation."
        )
    parts = did.split(":")
    if len(parts) != 4 or "/" not in parts[3]:
        raise ValueError(
            f"Malformed DID structure: {did!r}. "
            "Expected 'did:arc:{org}:{type}/{hash}'."
        )
    return did


def _did_to_filename(did: str) -> str:
    """Convert DID to a filesystem-safe filename component."""
    return did.replace(":", "_").replace("/", "_")


# ---------------------------------------------------------------------------
# AgentIdentity
# ---------------------------------------------------------------------------


class AgentIdentity:
    """Ed25519 identity with DID and sign/verify capabilities.

    Supports both full (signing + verify) and verify-only modes.
    Verify-only instances can be constructed from a public key alone;
    full instances require a SigningKey.

    This class is the canonical agent identity for all Arc packages.
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
        """Sign a message with Ed25519. Returns 64-byte signature bytes.

        Raises:
            ValueError: Identity has no private key (verify-only).
        """
        if self._signing_key is None:
            raise ValueError(
                "Cannot sign: no private key available (verify-only identity)"
            )
        signed = self._signing_key.sign(message)
        return signed.signature

    def verify(self, message: bytes, signature: bytes) -> bool:
        """Verify a signature against this identity's public key."""
        verify_key = VerifyKey(self.public_key)
        try:
            verify_key.verify(message, signature)
        except Exception:
            return False
        return True

    def save_keys(self, key_dir: Path) -> None:
        """Save keypair to filesystem with secure permissions.

        The key directory is created at 0700 (owner only); the key file
        is written at 0600. This is the minimum security posture for
        federal deployments.

        Raises:
            ValueError: Identity has no private key to save.
        """
        if self._signing_key is None:
            raise ValueError(
                "Cannot save keys: no private key available"
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

        Enforces 0600 permissions on the key file — insecure permissions
        indicate tampering or misconfiguration.

        Args:
            did: Full DID for the agent.
            key_dir: Directory containing the key files.

        Raises:
            ValueError: Key file missing or has insecure permissions.
        """
        key_dir = Path(key_dir)
        key_file = key_dir / f"{_did_to_filename(did)}.key"

        if not key_file.exists():
            raise ValueError(
                f"Key file not found: {key_file}"
            )

        file_mode = stat.S_IMODE(key_file.stat().st_mode)
        if file_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError(
                f"Key file has insecure permissions: {oct(file_mode)}. "
                "Expected 0o600 (owner read/write only)."
            )

        signing_key = SigningKey(key_file.read_bytes())
        return cls(
            did=did,
            public_key=bytes(signing_key.verify_key),
            _signing_key=signing_key,
        )

    @classmethod
    def generate(cls, org: str, agent_type: str) -> AgentIdentity:
        """Generate a new Ed25519 keypair and derive DID.

        Args:
            org: Deployment organization.
            agent_type: Agent role.

        Returns:
            New AgentIdentity with full signing capability.
        """
        kp = generate_keypair()
        signing_key = SigningKey(kp.private_key)
        did = generate_did(signing_key.verify_key, org=org, agent_type=agent_type)
        return cls(
            did=did,
            public_key=kp.public_key,
            _signing_key=signing_key,
        )

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        vault_resolver: Any = None,
        org: str = "default",
        agent_type: str = "executor",
        config_path: Path | None = None,
    ) -> AgentIdentity:
        """Resolve identity from config, generating and persisting if needed.

        Resolution order:
        1. ``config.did`` set → load from vault or file
        2. ``config.did`` empty → generate new keypair, save keys,
           write DID back into the config file

        The config file is the single source of truth for the agent's DID.

        Args:
            config: Object with ``did``, ``key_dir``, ``vault_path`` fields.
            vault_resolver: Optional vault backend (has ``resolve_secret``).
            org: Organization name (used when generating a new identity).
            agent_type: Agent role (used when generating a new identity).
            config_path: Path to the TOML config file for DID persistence.

        Raises:
            ValueError: Config DID is set but malformed, or key cannot be found.
        """
        key_dir = Path(config.key_dir).expanduser()
        did_to_load = validate_did(config.did)

        if did_to_load:
            if vault_resolver is not None and config.vault_path:
                try:
                    return cls._load_from_vault(
                        did_to_load, vault_resolver, config.vault_path
                    )
                except Exception:
                    _logger.warning(
                        "Vault lookup failed for %s, falling back to file",
                        did_to_load,
                    )
            return cls.load_keys(did_to_load, key_dir)

        # No DID — generate fresh identity
        identity = cls.generate(org=org, agent_type=agent_type)
        identity.save_keys(key_dir)

        if config_path is not None:
            cls._write_did_to_config(config_path, identity.did)

        _logger.info("Generated new identity: %s", identity.did)
        return identity

    @classmethod
    def _load_from_vault(
        cls, did: str, vault_resolver: Any, vault_path: str
    ) -> AgentIdentity:
        """Load signing key from vault backend."""
        key_hex = vault_resolver.resolve_secret(vault_path, did)
        signing_key = SigningKey(bytes.fromhex(key_hex))
        return cls(
            did=did,
            public_key=bytes(signing_key.verify_key),
            _signing_key=signing_key,
        )

    @staticmethod
    def _write_did_to_config(config_path: Path, did: str) -> None:
        """Write generated DID back into the agent's TOML config file."""
        try:
            content = config_path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            _logger.warning("Cannot read config to persist DID: %s", config_path)
            return

        updated = re.sub(
            r'^(\s*did\s*=\s*)["\'][\s]*["\']',
            rf'\1"{did}"',
            content,
            count=1,
            flags=re.MULTILINE,
        )

        if updated == content:
            _logger.warning(
                "Could not find empty did field in %s to update. "
                'Manually set: did = "%s"',
                config_path,
                did,
            )
            return

        config_path.write_text(updated, encoding="utf-8")
        _logger.info("Persisted DID to config: %s -> %s", config_path, did)


# ---------------------------------------------------------------------------
# ChildIdentity + derive_child_identity
# ---------------------------------------------------------------------------


class ChildIdentity(BaseModel):
    """Derived identity for a spawned child agent.

    Short-lived identity computed deterministically from the parent secret
    key and a per-spawn nonce. Cannot be reused across spawns.

    A Pydantic model so callers can use model_dump() / model_validate() for
    serialization (e.g. passing between processes or logging metadata).

    Attributes:
        did: DID in ``did:arc:delegate:child/{hex8}`` format.
        sk_bytes: 32-byte Ed25519 private key seed.
        ttl_s: Seconds until this identity expires.
    """

    did: str
    sk_bytes: bytes
    ttl_s: int


def derive_child_identity(
    parent_sk: bytes = b"",
    nonce: str = "",
    ttl_s: int = 300,
    *,
    parent_sk_bytes: bytes | None = None,
    spawn_id: str | None = None,
    wallclock_timeout_s: float | None = None,
) -> ChildIdentity:
    """Derive a deterministic child identity from parent secret key and nonce.

    Accepts both positional and keyword-only call styles for compatibility
    with the arcrun spawn module:
      - Positional: ``derive_child_identity(parent_sk, nonce, ttl_s)``
      - Keyword:    ``derive_child_identity(parent_sk_bytes=..., spawn_id=...)``

    Uses HKDF-SHA256 (simplified): PRK = HMAC-SHA256(salt, IKM),
    then T(1) = HMAC-SHA256(PRK, info || 0x01). The child seed is 32 bytes.

    Security:
    - ASI-03: child key is unique and unpredictable to anyone without parent SK.
    - Child key cannot be reused — nonce must be unique per spawn.

    Args:
        parent_sk: Parent's Ed25519 private key bytes (positional).
        nonce: Per-spawn nonce (positional).
        ttl_s: TTL in seconds (positional).
        parent_sk_bytes: Parent's Ed25519 private key bytes (keyword, wins over positional).
        spawn_id: Per-spawn nonce (keyword, wins over positional).
        wallclock_timeout_s: TTL in seconds (keyword, wins over positional).

    Returns:
        ChildIdentity with derived DID and 32-byte signing key.
    """
    # Keyword forms win when both provided (backward compat with arcrun API)
    effective_sk = parent_sk_bytes if parent_sk_bytes is not None else parent_sk
    effective_nonce = spawn_id if spawn_id is not None else nonce
    effective_ttl = (
        int(wallclock_timeout_s) if wallclock_timeout_s is not None else ttl_s
    )

    # HKDF-SHA256 expand step
    # PRK = HMAC-SHA256(salt="arc.spawn", IKM=parent_sk)
    # T(1) = HMAC-SHA256(PRK, info=nonce_bytes || 0x01)
    salt = b"arc.spawn"
    info = effective_nonce.encode("utf-8")
    prk = hmac.new(salt, effective_sk, hashlib.sha256).digest()
    child_seed = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()  # 32 bytes

    # 8 hex chars = 4 bytes (matches DID format: did:arc:delegate:child/{hex8})
    hex_suffix = child_seed[:4].hex()
    did = f"did:arc:delegate:child/{hex_suffix}"

    return ChildIdentity(did=did, sk_bytes=child_seed, ttl_s=effective_ttl)


__all__ = [
    "KEY_SIZE",
    "AgentIdentity",
    "ChildIdentity",
    "derive_child_identity",
    "generate_did",
    "parse_did",
    "validate_did",
]
