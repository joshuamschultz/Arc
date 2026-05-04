"""Single source of truth for the viewer-DID derivation formula.

When arcui mints a viewer token (operator login), every component down
the pipeline — adapter, SessionRouter, audit chain, pairing — must agree
on the DID derived from that token. Putting the formula in one tiny
module means there is exactly one place to swap when arctrust DID
issuance ships:

    return arctrust.resolve_or_issue(viewer_token)

Today's formula is a deterministic hash truncation: stable per token,
secret-free at the call site (the adapter never sees the raw token),
collision-resistant at 2^64 preimage strength.

Format: ``did:arc:viewer:<16 lowercase hex chars>``.
"""

from __future__ import annotations

import hashlib

_VIEWER_PREFIX = "did:arc:viewer:"


def derive_viewer_did(viewer_token: str) -> str:
    """Return a stable DID for an arcui-issued viewer token.

    Args:
        viewer_token: Opaque token minted by arcui at operator login. Never
            logged, never forwarded to adapters; this function is the only
            place that touches it.

    Returns:
        DID string of the form ``did:arc:viewer:<16 lowercase hex chars>``.
        Same input always produces the same output.
    """
    digest = hashlib.sha256(viewer_token.encode("utf-8")).hexdigest()
    return _VIEWER_PREFIX + digest[:16]
