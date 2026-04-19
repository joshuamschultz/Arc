"""arctrust — Ed25519 trust-store primitives shared across Arc packages.

This package provides the authoritative source for loading and caching
operator and manifest-issuer Ed25519 public keys from TOML trust files.
Both arcagent and arcrun depend on this package; neither depends on the
other for trust-store needs, eliminating the latent circular dependency
documented in SPEC-018 §HIGH-1.

Public surface:
    TrustStoreError         — structured error with code + details
    load_operator_pubkey    — load Ed25519 pubkey for an operator DID
    load_issuer_pubkey      — load Ed25519 pubkey for a manifest-issuer DID
    invalidate_cache        — flush the in-process TTL cache
"""

from arctrust.trust_store import (
    TrustStoreError,
    invalidate_cache,
    load_issuer_pubkey,
    load_operator_pubkey,
)

__all__ = [
    "TrustStoreError",
    "invalidate_cache",
    "load_issuer_pubkey",
    "load_operator_pubkey",
]
