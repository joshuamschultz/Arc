"""COMP-001 / REQ-122,124,125,126,129: two-layer resolution + fail-loud."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcprompt.errors import PromptMissing, PromptUnsigned
from arcprompt.resolver import PromptResolver
from arcprompt.verifier import TrustPosture
from tests.conftest import DirCatalog, SigningKey, write_overlay, write_stock


def _resolver(overlay_root: Path, stock_root: Path, signer: SigningKey) -> PromptResolver:
    return PromptResolver(
        overlay_root=overlay_root,
        trusted_public_key=signer.public_key,
        posture=TrustPosture.FEDERAL,
        catalog=DirCatalog(stock_root),
    )


def test_absent_overlay_resolves_to_stock_silently(tmp_path: Path, signer: SigningKey) -> None:
    overlay_root = tmp_path / "overlays"
    stock_root = tmp_path / "stock"
    write_stock(stock_root, "arcrun", "greeting", "stock body")
    doc = _resolver(overlay_root, stock_root, signer).resolve("arcrun", "greeting")
    assert doc.source == "stock"
    assert doc.body == "stock body"
    assert doc.signer_did is None


def test_valid_overlay_wins_over_stock(tmp_path: Path, signer: SigningKey) -> None:
    overlay_root = tmp_path / "overlays"
    stock_root = tmp_path / "stock"
    write_stock(stock_root, "arcrun", "greeting", "stock body")
    write_overlay(overlay_root, "arcrun", "greeting", "overlay body", signer=signer)
    doc = _resolver(overlay_root, stock_root, signer).resolve("arcrun", "greeting")
    assert doc.source == "overlay"
    assert doc.body == "overlay body"
    assert doc.signer_did == signer.did


def test_unsigned_overlay_raises_never_falls_back(tmp_path: Path, signer: SigningKey) -> None:
    overlay_root = tmp_path / "overlays"
    stock_root = tmp_path / "stock"
    write_stock(stock_root, "arcrun", "greeting", "stock body")
    write_overlay(overlay_root, "arcrun", "greeting", "overlay body", signer=signer, sign=False)
    with pytest.raises(PromptUnsigned):
        _resolver(overlay_root, stock_root, signer).resolve("arcrun", "greeting")


def test_wrong_key_overlay_raises_never_falls_back(tmp_path: Path, signer: SigningKey) -> None:
    import os

    from arctrust.keypair import KeyPair

    overlay_root = tmp_path / "overlays"
    stock_root = tmp_path / "stock"
    write_stock(stock_root, "arcrun", "greeting", "stock body")
    attacker_seed = os.urandom(32)
    attacker = SigningKey(
        seed=attacker_seed,
        public_key=KeyPair.from_seed(attacker_seed).public_key,
        did="did:arc:attacker",
    )
    write_overlay(overlay_root, "arcrun", "greeting", "evil body", signer=attacker)
    # Resolver pins the legitimate key; the attacker's self-signature must not pass.
    with pytest.raises(PromptUnsigned):
        _resolver(overlay_root, stock_root, signer).resolve("arcrun", "greeting")


def test_missing_stock_raises_packaging_error(tmp_path: Path, signer: SigningKey) -> None:
    overlay_root = tmp_path / "overlays"
    stock_root = tmp_path / "stock"
    with pytest.raises(PromptMissing) as exc:
        _resolver(overlay_root, stock_root, signer).resolve("arcrun", "nonexistent")
    assert exc.value.package == "arcrun"
    assert exc.value.name == "nonexistent"


@pytest.mark.parametrize(
    ("package", "name"),
    [
        ("arcrun", "../../../etc/passwd"),
        ("arcrun", ".."),
        ("arcrun", "a/b"),
        ("arcrun", "x\\y"),
        ("arcrun", ""),
        ("../../evil", "greeting"),
        ("arcrun", "with\x00null"),
    ],
)
def test_resolve_refuses_path_traversal_components(
    tmp_path: Path, signer: SigningKey, package: str, name: str
) -> None:
    """SEC-04: a package/name that could traverse out of the roots is refused, never read."""
    overlay_root = tmp_path / "overlays"
    stock_root = tmp_path / "stock"
    with pytest.raises(PromptMissing):
        _resolver(overlay_root, stock_root, signer).resolve(package, name)


def test_corrupt_signature_sidecar_raises_unsigned(tmp_path: Path, signer: SigningKey) -> None:
    """A `.arcsig` sidecar with non-JSON garbage fails loud, never falls back to stock."""
    overlay_root = tmp_path / "overlays"
    stock_root = tmp_path / "stock"
    write_stock(stock_root, "arcrun", "greeting", "stock body")
    md = write_overlay(overlay_root, "arcrun", "greeting", "overlay body", signer=signer)
    (md.parent / "greeting.md.arcsig").write_text("not-valid-json{{{", encoding="utf-8")
    with pytest.raises(PromptUnsigned):
        _resolver(overlay_root, stock_root, signer).resolve("arcrun", "greeting")


def test_none_pin_with_overlay_present_fails_closed(tmp_path: Path, signer: SigningKey) -> None:
    overlay_root = tmp_path / "overlays"
    stock_root = tmp_path / "stock"
    write_stock(stock_root, "arcrun", "greeting", "stock body")
    write_overlay(overlay_root, "arcrun", "greeting", "overlay body", signer=signer)
    resolver = PromptResolver(
        overlay_root=overlay_root,
        trusted_public_key=None,
        posture=TrustPosture.PERSONAL,
        catalog=DirCatalog(stock_root),
    )
    with pytest.raises(PromptUnsigned):
        resolver.resolve("arcrun", "greeting")
