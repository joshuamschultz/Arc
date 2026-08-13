"""Bundle lifecycle audit events, asserted against the real WORM chain.

These drive a genuine ``arctrust.WormSink`` — the deployment's compliance system
of record — rather than a stubbed ``emit``. A test that patched emission would
still pass if the event never reached a sink, which is the exact failure an audit
control exists to prevent. Every assertion below reads the signed chain back off
disk, and one verifies the chain's signatures and hash links.

The second property under test is *where* the events come from. arcbundle emits
each one at the point the outcome is decided, so the CLI, release CI, and any
later surface record the same fact for the same bundle instead of inventing
their own vocabulary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arctrust import InProcessSigner, WormSink, generate_keypair, verify_chain

from arcbundle import (
    BundleContentHashError,
    BundleMaterializeError,
    BundleSignatureError,
    materialize,
    remove,
    verify_bundle,
)

_OPERATOR = "did:arc:org:operator/test"


@pytest.fixture
def chain(tmp_path: Path) -> Iterator[tuple[WormSink, Path, bytes]]:
    """A real operator-signed WORM chain — the sink, its file, and its pubkey."""
    signer = InProcessSigner(generate_keypair().private_key)
    path = tmp_path / "audit" / "chain.jsonl"
    sink = WormSink(path, signer)
    try:
        yield sink, path, signer.public_key
    finally:
        sink.close()


def _events(path: Path) -> list[dict[str, Any]]:
    """Read the signed chain back as the events an auditor would see."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["event"] for line in lines if line.strip()]


def _actions(path: Path) -> list[str]:
    return [event["action"] for event in _events(path)]


def _only(path: Path, action: str) -> dict[str, Any]:
    """The single event with ``action`` — asserting there is exactly one.

    One emission point per outcome is the property; a duplicate would mean two
    call sites recording the same fact, which is how two surfaces start to
    disagree.
    """
    matches = [event for event in _events(path) if event["action"] == action]
    assert len(matches) == 1, f"expected exactly one {action}, got {len(matches)}"
    return matches[0]


def test_verify_emits_bundle_verified_naming_bundle_module_and_issuer(
    tmp_path: Path, make_bundle: Any, chain: tuple[WormSink, Path, bytes]
) -> None:
    sink, path, _ = chain
    bundle = make_bundle(tmp_path / "browser.arcbundle", module="browser", version="2.1.0")

    verify_bundle(
        bundle.path,
        tier="personal",
        trusted_issuers=bundle.trusted(),
        sink=sink,
        actor_did=_OPERATOR,
    )

    event = _only(path, "module.bundle.verified")
    assert event["target"] == "browser"
    assert event["extra"]["bundle"] == str(bundle.path)
    assert event["extra"]["issuer"] == bundle.issuer
    assert event["extra"]["version"] == "2.1.0"
    assert event["outcome"] == "allow"
    assert event["actor_did"] == _OPERATOR
    assert event["tier"] == "personal"


def test_untrusted_issuer_emits_signature_invalid_and_nothing_else(
    tmp_path: Path, make_bundle: Any, chain: tuple[WormSink, Path, bytes]
) -> None:
    """A refusal must record the refusal — and must not also record a pass."""
    sink, path, _ = chain
    bundle = make_bundle(tmp_path / "browser.arcbundle", module="browser")

    with pytest.raises(BundleSignatureError):
        verify_bundle(bundle.path, tier="personal", trusted_issuers={}, sink=sink)

    assert _actions(path) == ["module.signature_invalid"]
    event = _only(path, "module.signature_invalid")
    assert event["target"] == "browser"
    assert event["extra"]["issuer"] == bundle.issuer
    assert event["extra"]["bundle"] == str(bundle.path)
    assert event["outcome"] == "deny"


def test_dev_issuer_at_federal_emits_signature_invalid(
    tmp_path: Path, make_bundle: Any, chain: tuple[WormSink, Path, bytes]
) -> None:
    """The dev-key refusal is a security decision, so it has to be auditable as
    one rather than surfacing only as a CLI message."""
    sink, path, _ = chain
    bundle = make_bundle(tmp_path / "browser.arcbundle", module="browser", issuer="did:arc:dev")

    with pytest.raises(BundleSignatureError):
        verify_bundle(bundle.path, tier="federal", trusted_issuers=bundle.trusted(), sink=sink)

    assert _only(path, "module.signature_invalid")["extra"]["issuer"] == "did:arc:dev"


def test_tampered_payload_emits_content_hash_mismatch(
    tmp_path: Path, make_bundle: Any, chain: tuple[WormSink, Path, bytes]
) -> None:
    sink, path, _ = chain
    bundle = make_bundle(tmp_path / "browser.arcbundle", module="browser")
    bundle.payload_file("tools/fetch.py").write_bytes(b"# swapped after signing\n")

    with pytest.raises(BundleContentHashError):
        verify_bundle(bundle.path, tier="personal", trusted_issuers=bundle.trusted(), sink=sink)

    assert _actions(path) == ["module.content_hash_mismatch"]
    event = _only(path, "module.content_hash_mismatch")
    assert event["target"] == "browser"
    assert event["extra"]["issuer"] == bundle.issuer


def test_install_and_remove_emit_one_event_each_on_a_verifiable_chain(
    tmp_path: Path, make_bundle: Any, chain: tuple[WormSink, Path, bytes]
) -> None:
    """The full operator arc: verify, install, remove — and the chain that
    records it still verifies afterwards (AU-10)."""
    sink, path, public_key = chain
    dest = tmp_path / "modules"
    bundle = make_bundle(tmp_path / "browser.arcbundle", module="browser", version="2.1.0")

    verified = verify_bundle(
        bundle.path,
        tier="personal",
        trusted_issuers=bundle.trusted(),
        sink=sink,
        actor_did=_OPERATOR,
    )
    installed = materialize(verified, dest, sink=sink, actor_did=_OPERATOR)
    remove("browser", dest, sink=sink, actor_did=_OPERATOR)

    assert _actions(path) == [
        "module.bundle.verified",
        "module.installed",
        "module.removed",
    ]
    install_event = _only(path, "module.installed")
    assert install_event["target"] == "browser"
    assert install_event["extra"]["issuer"] == bundle.issuer
    assert install_event["extra"]["bundle"] == str(bundle.path)
    assert install_event["extra"]["path"] == str(installed)
    assert _only(path, "module.removed")["extra"]["path"] == str(installed)
    assert verify_chain(path, public_key) is True


def test_a_refused_install_records_no_installed_event(
    tmp_path: Path, chain: tuple[WormSink, Path, bytes]
) -> None:
    """`module.installed` must mean bytes reached disk. Removing something that
    was never there is a failure, and the chain has to show the absence."""
    sink, path, _ = chain

    with pytest.raises(BundleMaterializeError):
        remove("browser", tmp_path / "modules", sink=sink)

    assert _events(path) == []


def test_manifest_bytes_are_unchanged_by_being_audited(
    tmp_path: Path, make_bundle: Any, chain: tuple[WormSink, Path, bytes]
) -> None:
    """Verification reads; auditing it must not turn that into a write."""
    sink, _, _ = chain
    bundle = make_bundle(tmp_path / "browser.arcbundle", module="browser")
    before = {
        str(entry.relative_to(bundle.path)): hashlib.sha256(entry.read_bytes()).hexdigest()
        for entry in sorted(bundle.path.rglob("*"))
        if entry.is_file()
    }

    verify_bundle(bundle.path, tier="personal", trusted_issuers=bundle.trusted(), sink=sink)

    after = {
        str(entry.relative_to(bundle.path)): hashlib.sha256(entry.read_bytes()).hexdigest()
        for entry in sorted(bundle.path.rglob("*"))
        if entry.is_file()
    }
    assert after == before
