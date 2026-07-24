"""COMP-004 / REQ-123,131,132: run-start freeze + single provenance audit event."""

from __future__ import annotations

from pathlib import Path

from arctrust.artifact import sign_artifact
from arctrust.audit import AuditEvent

from arcprompt.catalog import PromptRef
from arcprompt.document import render_prompt
from arcprompt.resolver import PromptResolver
from arcprompt.snapshot import PROVENANCE_ACTION, snapshot
from arcprompt.verifier import TrustPosture
from tests.conftest import DirCatalog, SigningKey, write_overlay, write_stock


class _CollectingSink:
    """Minimal audit sink capturing every event for assertion."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _resolver(overlay_root: Path, stock_root: Path, signer: SigningKey, posture: TrustPosture):
    return PromptResolver(
        overlay_root=overlay_root,
        trusted_public_key=signer.public_key,
        posture=posture,
        catalog=DirCatalog(stock_root),
    )


def test_snapshot_is_frozen_for_the_run(tmp_path: Path, signer: SigningKey) -> None:
    overlay_root = tmp_path / "overlays"
    stock_root = tmp_path / "stock"
    write_stock(stock_root, "arcrun", "greeting", "stock body")
    overlay = write_overlay(overlay_root, "arcrun", "greeting", "overlay v1", signer=signer)
    resolver = _resolver(overlay_root, stock_root, signer, TrustPosture.FEDERAL)
    refs = [PromptRef(package="arcrun", name="greeting", description="d", stock_path=overlay)]

    snap1 = snapshot(resolver, refs, actor_did="did:arc:agent", sink=_CollectingSink())
    assert snap1.get("arcrun", "greeting").body == "overlay v1"

    # Mutate the overlay mid-run: the frozen snapshot must not change.
    raw2 = render_prompt("overlay v2", name="greeting", description="d")
    overlay.write_bytes(raw2)
    (overlay.parent / "greeting.md.arcsig").write_text(
        sign_artifact(raw2, signer_did=signer.did, private_key=signer.seed).to_json(),
        encoding="utf-8",
    )
    assert snap1.get("arcrun", "greeting").body == "overlay v1"  # still the frozen value

    # The NEXT run observes the change.
    snap2 = snapshot(resolver, refs, actor_did="did:arc:agent", sink=_CollectingSink())
    assert snap2.get("arcrun", "greeting").body == "overlay v2"


def test_exactly_one_provenance_event_with_resolved_values(
    tmp_path: Path, signer: SigningKey
) -> None:
    overlay_root = tmp_path / "overlays"
    stock_root = tmp_path / "stock"
    stock = write_stock(stock_root, "arcagent", "spawn", "stock spawn")
    overlay = write_overlay(overlay_root, "arcrun", "greeting", "overlay body", signer=signer)
    resolver = _resolver(overlay_root, stock_root, signer, TrustPosture.FEDERAL)
    refs = [
        PromptRef(package="arcrun", name="greeting", description="d", stock_path=overlay),
        PromptRef(package="arcagent", name="spawn", description="d", stock_path=stock),
    ]
    sink = _CollectingSink()
    snapshot(resolver, refs, actor_did="did:arc:agent", sink=sink)

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.action == PROVENANCE_ACTION
    assert event.actor_did == "did:arc:agent"
    rows = event.extra["prompts"]
    assert len(rows) == 2
    by_key = {(r["package"], r["name"]): r for r in rows}
    assert by_key[("arcrun", "greeting")]["source"] == "overlay"
    assert by_key[("arcrun", "greeting")]["signer_did"] == signer.did
    assert by_key[("arcagent", "spawn")]["source"] == "stock"
    assert by_key[("arcagent", "spawn")]["signer_did"] is None
    assert all(r["sha256"].startswith("sha256:") for r in rows)


def test_snapshot_container_protocol(tmp_path: Path, signer: SigningKey) -> None:
    stock_root = tmp_path / "stock"
    stock = write_stock(stock_root, "arcrun", "greeting", "body")
    resolver = _resolver(tmp_path / "ov", stock_root, signer, TrustPosture.PERSONAL)
    refs = [PromptRef(package="arcrun", name="greeting", description="d", stock_path=stock)]
    snap = snapshot(resolver, refs, actor_did="did:arc:agent", sink=_CollectingSink())
    assert ("arcrun", "greeting") in snap
    assert ("arcrun", "missing") not in snap
    assert len(snap) == 1


def test_federal_event_never_records_a_personal_or_default_value(
    tmp_path: Path, signer: SigningKey
) -> None:
    """The SPEC-017 audit-lies regression: resolved values only, never a default."""
    overlay_root = tmp_path / "overlays"
    stock_root = tmp_path / "stock"
    stock = write_stock(stock_root, "arcrun", "greeting", "stock body")
    resolver = _resolver(overlay_root, stock_root, signer, TrustPosture.FEDERAL)
    refs = [PromptRef(package="arcrun", name="greeting", description="d", stock_path=stock)]
    sink = _CollectingSink()
    snapshot(resolver, refs, actor_did="did:arc:fed-agent", sink=sink)

    event = sink.events[0]
    assert event.tier == "federal"  # resolved posture, not a hardcoded 'personal'
    # A stock prompt has no signer; the row must say so explicitly, never a default DID.
    assert event.extra["prompts"][0]["signer_did"] is None


def test_snapshot_payload_holds_all_prompts_without_truncation(
    tmp_path: Path, signer: SigningKey
) -> None:
    """~25 prompts must all land in one event (T-744 sink-truncation guard)."""
    overlay_root = tmp_path / "overlays"
    stock_root = tmp_path / "stock"
    refs = []
    for i in range(30):
        stock = write_stock(stock_root, "arcrun", f"p{i:02d}", f"body {i}")
        refs.append(
            PromptRef(package="arcrun", name=f"p{i:02d}", description="d", stock_path=stock)
        )
    resolver = _resolver(overlay_root, stock_root, signer, TrustPosture.PERSONAL)
    sink = _CollectingSink()
    snapshot(resolver, refs, actor_did="did:arc:agent", sink=sink)
    assert len(sink.events[0].extra["prompts"]) == 30
