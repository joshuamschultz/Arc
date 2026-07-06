"""SPEC-033 F2/REQ-051 — pluggable trust backend behind one verify Protocol.

The capability loader depends only on the :class:`TrustBackend` Protocol.
Swapping the Ed25519/DID backend for another (e.g. Sigstore) requires no loader
change — both satisfy the same ``verify`` signature.
"""

from __future__ import annotations

from pathlib import Path

from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing
from arcagent.capabilities.trust_backend import Ed25519TrustBackend, TrustBackend


def test_ed25519_backend_satisfies_protocol() -> None:
    backend = Ed25519TrustBackend()
    assert isinstance(backend, TrustBackend)


def test_ed25519_backend_verifies_signed_artifact(tmp_path: Path) -> None:
    ident = AgentIdentity.generate(org="arc", agent_type="exec")
    artifact = tmp_path / "hello.py"
    content = b"async def fn(): return 1\n"
    artifact.write_bytes(content)
    artifact_signing.write_signature(
        artifact, content, signer_did=ident.did, private_key=ident.signing_seed
    )
    backend = Ed25519TrustBackend()
    assert backend.verify(artifact, content, trusted_public_key=ident.public_key) is True
    assert backend.verify(artifact, b"tampered", trusted_public_key=ident.public_key) is False


def test_a_stub_backend_also_satisfies_protocol() -> None:
    class AlwaysDeny:
        def verify(
            self, artifact: Path, content: bytes, *, trusted_public_key: bytes | None = None
        ) -> bool:
            return False

    assert isinstance(AlwaysDeny(), TrustBackend)
