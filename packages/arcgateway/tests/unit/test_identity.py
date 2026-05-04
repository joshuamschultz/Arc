"""Tests for arcgateway.identity.derive_viewer_did.

The single source of truth for the viewer-DID derivation formula. Three
properties matter:
1. Determinism — same input → same output, every time.
2. Format     — ``did:arc:viewer:<16 lowercase hex chars>``.
3. Distinctness — different inputs → different outputs (collision-resistant
   modulo SHA-256's preimage strength, truncated to 64 bits).
"""

from __future__ import annotations

import re

from arcgateway.identity import derive_viewer_did

_DID_RE = re.compile(r"^did:arc:viewer:[0-9a-f]{16}$")


def test_derive_viewer_did_is_deterministic() -> None:
    """Same input always produces the same DID."""
    token = "viewer-token-abc-123"
    a = derive_viewer_did(token)
    b = derive_viewer_did(token)
    assert a == b


def test_derive_viewer_did_format_matches_regex() -> None:
    """Output matches ``did:arc:viewer:[0-9a-f]{16}`` exactly."""
    did = derive_viewer_did("any-token")
    assert _DID_RE.match(did) is not None, f"DID format mismatch: {did!r}"


def test_derive_viewer_did_distinct_inputs_produce_distinct_outputs() -> None:
    """Two distinct tokens map to distinct DIDs."""
    a = derive_viewer_did("token-one")
    b = derive_viewer_did("token-two")
    assert a != b
