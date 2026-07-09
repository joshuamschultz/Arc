"""Cross-package byte-identity for the signing serializer.

arcagent depends on arcllm, arcrun, and arctrust, so it is the one place that can
assert every package's signing-payload helper produces *byte-identical* output
to :func:`arctrust.canonical_json`. If any helper drifts (compact-vs-default
separators, ensure_ascii, key order), a signature made in one package stops
verifying in another — one of these assertions catches it.
"""

from __future__ import annotations

from arcllm._signing import canonical_payload
from arcllm.types import Message
from arcrun.backends._verifier import canonical_json_payload
from arctrust import canonical_json

from arcagent.tools.checkpoint_signing import _canonical_bytes


def test_arcllm_request_signing_matches_canonical_json() -> None:
    messages = [Message(role="user", content="hi")]
    expected = canonical_json(
        {
            "messages": [m.model_dump() for m in messages],
            "model": "gpt-x",
            "tools": [],
        }
    )
    assert canonical_payload(messages, None, "gpt-x") == expected


def test_arcagent_checkpoint_signing_matches_canonical_json() -> None:
    record = {
        "type": "checkpoint",
        "timestamp": "2026-01-01T00:00:00Z",
        "signature": "deadbeef",
        "tokens_used": 42,
        "cost_usd": 1.5,
    }
    # Envelope keys (type/timestamp/signature) are excluded from the signed bytes.
    expected = canonical_json({"tokens_used": 42, "cost_usd": 1.5})
    assert _canonical_bytes(record) == expected


def test_arcrun_manifest_signing_matches_canonical_json() -> None:
    meta = {"issuer_did": "did:arc:org:agent:hash"}
    backends = [{"name": "vm", "module": "arcrun.backends.vm", "content_hash": "sha256:00"}]
    expected = canonical_json({"meta": meta, "backends": backends})
    assert canonical_json_payload(meta=meta, backends=backends) == expected


def test_all_signing_helpers_agree_on_shared_fixture() -> None:
    # A single fixture serialized through every path yields identical bytes —
    # the concrete guarantee that a signature is portable across packages.
    meta = {"issuer_did": "did:arc:x"}
    backends = [{"name": "b"}]
    reference = canonical_json({"meta": meta, "backends": backends})
    assert canonical_json_payload(meta=meta, backends=backends) == reference
    assert _canonical_bytes({"meta": meta, "backends": backends}) == reference
