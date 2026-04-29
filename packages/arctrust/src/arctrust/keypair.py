"""Ed25519 keypair primitives — generate, sign, verify.

This is the cryptographic foundation for all identity operations in Arc.
All other identity modules build on these primitives; nothing else in
arctrust should import PyNaCl directly.

Security properties:
- Ed25519 signatures are deterministic (no per-signature randomness needed).
- 32-byte keys; 64-byte signatures. No other sizes accepted.
- verify() NEVER raises — it returns False on any invalid input, including
  malformed keys or signatures. Callers that need exception semantics must
  wrap it themselves.
- sign() DOES raise ValueError on a bad private key (callers must supply
  a valid 32-byte seed; invalid input is a programming error, not a runtime
  condition).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

_logger = logging.getLogger("arctrust.keypair")

# Ed25519 constants — documented here so callers don't hard-code magic numbers.
KEY_SIZE = 32
SIGNATURE_SIZE = 64


@dataclass(frozen=True)
class KeyPair:
    """Immutable Ed25519 keypair.

    Attributes:
        public_key: 32-byte Ed25519 verify key.
        private_key: 32-byte Ed25519 signing key seed.
    """

    public_key: bytes
    private_key: bytes

    @classmethod
    def from_seed(cls, seed: bytes) -> KeyPair:
        """Reconstruct a KeyPair deterministically from a 32-byte seed.

        Args:
            seed: 32-byte Ed25519 private key seed.

        Returns:
            KeyPair with the corresponding public key.

        Raises:
            ValueError: Seed is not exactly 32 bytes.
        """
        if len(seed) != KEY_SIZE:
            raise ValueError(f"Ed25519 seed must be {KEY_SIZE} bytes, got {len(seed)}")
        sk = SigningKey(seed)
        return cls(
            public_key=bytes(sk.verify_key),
            private_key=seed,
        )


def generate_keypair() -> KeyPair:
    """Generate a fresh Ed25519 keypair using a cryptographically secure RNG.

    Returns:
        KeyPair with 32-byte public and private keys.
    """
    sk = SigningKey.generate()
    return KeyPair(
        public_key=bytes(sk.verify_key),
        private_key=sk.encode(),
    )


def sign(message: bytes, private_key: bytes) -> bytes:
    """Sign a message with an Ed25519 private key.

    Args:
        message: Arbitrary bytes to sign.
        private_key: 32-byte Ed25519 signing key seed.

    Returns:
        64-byte Ed25519 signature.

    Raises:
        ValueError: private_key is not exactly 32 bytes.
    """
    if len(private_key) != KEY_SIZE:
        raise ValueError(f"Ed25519 private key must be {KEY_SIZE} bytes, got {len(private_key)}")
    sk = SigningKey(private_key)
    signed = sk.sign(message)
    # PyNaCl returns the signature (not the signed message) via .signature
    return signed.signature


def verify(message: bytes, signature: bytes, public_key: bytes) -> bool:
    """Verify an Ed25519 signature.

    Returns False for any invalid input (wrong key length, bad signature,
    tampered message). Never raises — all errors collapse to False so
    callers can use simple boolean checks without try/except.

    Args:
        message: The original message bytes.
        signature: 64-byte Ed25519 signature.
        public_key: 32-byte Ed25519 verify key.

    Returns:
        True if the signature is valid; False otherwise.
    """
    if len(public_key) != KEY_SIZE or len(signature) != SIGNATURE_SIZE:
        return False
    try:
        vk = VerifyKey(public_key)
        vk.verify(message, signature)
        return True
    except (BadSignatureError, Exception):
        # Catch-all is intentional: PyNaCl can raise ValueError for bad key material.
        # Every failure path is False — no exception semantics for callers.
        _logger.debug("verify() returning False due to signature check failure")
        return False


__all__ = [
    "KEY_SIZE",
    "SIGNATURE_SIZE",
    "KeyPair",
    "generate_keypair",
    "sign",
    "verify",
]
