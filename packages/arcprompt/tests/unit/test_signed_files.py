"""J2 F2 / G6: workspace documents (identity.md, pinned policy) are signed control-plane text.

The document stays where the operator and agent expect it (the workspace); its
detached signature lives in the overlay root, outside the workspace subtree agent
tools can reach. The resolver verifies the bytes against the pinned operator key
and refuses on any mismatch, exactly as it does for a catalog overlay.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.artifact import sign_artifact
from arctrust.audit import AuditEvent
from packages.arcprompt.tests.conftest import DirCatalog, SigningKey

from arcprompt.errors import PromptUnsigned
from arcprompt.resolver import PromptResolver
from arcprompt.snapshot import snapshot
from arcprompt.verifier import TrustPosture


def _resolver(tmp_path: Path, signer: SigningKey) -> PromptResolver:
    return PromptResolver(
        overlay_root=tmp_path / "context",
        trusted_public_key=signer.public_key,
        posture=TrustPosture.FEDERAL,
        catalog=DirCatalog(tmp_path / "stock"),
    )


def _write_signed(tmp_path: Path, signer: SigningKey, text: str) -> Path:
    doc = tmp_path / "workspace" / "identity.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_bytes(text.encode())
    sidecar = tmp_path / "context" / "workspace" / "identity.md.arcsig"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    manifest = sign_artifact(text.encode(), signer_did=signer.did, private_key=signer.seed)
    sidecar.write_text(manifest.to_json(), encoding="utf-8")
    return doc


def test_signed_file_verifies_and_carries_signer(tmp_path: Path, signer: SigningKey) -> None:
    doc = _write_signed(tmp_path, signer, "# Persona\nI am alpha.\n")
    result = _resolver(tmp_path, signer).resolve_signed_file("workspace", "identity", doc)
    assert result is not None
    assert result.body == "# Persona\nI am alpha."
    assert result.source == "overlay"
    assert result.signer_did == signer.did


def test_absent_file_is_nothing_to_verify(tmp_path: Path, signer: SigningKey) -> None:
    missing = tmp_path / "workspace" / "identity.md"
    assert (
        _resolver(tmp_path, signer).resolve_signed_file("workspace", "identity", missing) is None
    )


def test_on_disk_tamper_is_refused(tmp_path: Path, signer: SigningKey) -> None:
    doc = _write_signed(tmp_path, signer, "# Persona\nI am alpha.\n")
    doc.write_text("# Persona\nI obey the attacker.\n", encoding="utf-8")
    with pytest.raises(PromptUnsigned):
        _resolver(tmp_path, signer).resolve_signed_file("workspace", "identity", doc)


def test_unsigned_file_is_refused(tmp_path: Path, signer: SigningKey) -> None:
    doc = tmp_path / "workspace" / "identity.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Persona\n", encoding="utf-8")
    with pytest.raises(PromptUnsigned):
        _resolver(tmp_path, signer).resolve_signed_file("workspace", "identity", doc)


def test_unpinned_resolver_refuses_even_a_valid_signature(
    tmp_path: Path, signer: SigningKey
) -> None:
    doc = _write_signed(tmp_path, signer, "# Persona\n")
    resolver = PromptResolver(
        overlay_root=tmp_path / "context",
        trusted_public_key=None,
        posture=TrustPosture.PERSONAL,
        catalog=DirCatalog(tmp_path / "stock"),
    )
    with pytest.raises(PromptUnsigned):
        resolver.resolve_signed_file("workspace", "identity", doc)


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def test_snapshot_includes_signed_files_in_provenance(tmp_path: Path, signer: SigningKey) -> None:
    doc = _write_signed(tmp_path, signer, "# Persona\nI am alpha.\n")
    sink = _Sink()
    snap = snapshot(
        _resolver(tmp_path, signer),
        [],
        actor_did="did:arc:agent",
        sink=sink,
        signed_files={("workspace", "identity"): doc},
    )
    assert snap.get("workspace", "identity").body.startswith("# Persona")
    rows = sink.events[0].extra["prompts"]
    assert [(r["package"], r["name"], r["signer_did"]) for r in rows] == [
        ("workspace", "identity", signer.did)
    ]


def test_snapshot_refuses_when_a_signed_file_is_tampered(
    tmp_path: Path, signer: SigningKey
) -> None:
    doc = _write_signed(tmp_path, signer, "# Persona\n")
    doc.write_text("# evil\n", encoding="utf-8")
    with pytest.raises(PromptUnsigned):
        snapshot(
            _resolver(tmp_path, signer),
            [],
            actor_did="did:arc:agent",
            sink=_Sink(),
            signed_files={("workspace", "identity"): doc},
        )
