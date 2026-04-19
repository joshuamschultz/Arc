"""Backward-compatibility shim — re-exports arctrust.trust_store.

The implementation now lives in the dedicated ``arctrust`` package so that
both arcagent and arcrun can depend on it without a circular import
(SPEC-018 HIGH-1).  All public names are preserved at this import path.
"""

from arctrust.trust_store import (  # noqa: F401
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
