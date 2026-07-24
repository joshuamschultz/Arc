"""Shared fixtures for arcprompt tests: real Ed25519 keys and signed overlays."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from arctrust.artifact import sign_artifact
from arctrust.keypair import KeyPair

from arcprompt.catalog import PromptCatalog, PromptRef
from arcprompt.document import parse_prompt, render_prompt


@dataclass(frozen=True)
class SigningKey:
    """A test signing identity: the Ed25519 seed, its public key, and a DID."""

    seed: bytes
    public_key: bytes
    did: str


@pytest.fixture
def signer() -> SigningKey:
    seed = os.urandom(32)
    return SigningKey(seed=seed, public_key=KeyPair.from_seed(seed).public_key, did="did:arc:test")


def write_overlay(
    overlay_root: Path,
    package: str,
    name: str,
    body: str,
    *,
    signer: SigningKey,
    description: str = "overlay description",
    sign: bool = True,
) -> Path:
    """Write a (optionally signed) overlay under ``overlay_root/package/name.md``."""
    raw = render_prompt(body, name=name, description=description)
    pkg_dir = overlay_root / package
    pkg_dir.mkdir(parents=True, exist_ok=True)
    md = pkg_dir / f"{name}.md"
    md.write_bytes(raw)
    if sign:
        manifest = sign_artifact(raw, signer_did=signer.did, private_key=signer.seed)
        (pkg_dir / f"{name}.md.arcsig").write_text(manifest.to_json(), encoding="utf-8")
    return md


def write_stock(stock_root: Path, package: str, name: str, body: str) -> Path:
    """Write a stock prompt file under ``stock_root/package/name.md``."""
    raw = render_prompt(body, name=name, description="stock description")
    pkg_dir = stock_root / package
    pkg_dir.mkdir(parents=True, exist_ok=True)
    md = pkg_dir / f"{name}.md"
    md.write_bytes(raw)
    return md


class DirCatalog(PromptCatalog):
    """A catalog rooted at a plain directory tree, for tests without a real wheel."""

    def __init__(self, stock_root: Path) -> None:
        self._root = stock_root

    def catalog(self) -> list[PromptRef]:
        refs: list[PromptRef] = []
        for md in sorted(self._root.rglob("*.md")):
            doc = parse_prompt(md.read_bytes(), source="stock")
            refs.append(
                PromptRef(
                    package=md.parent.name,
                    name=doc.name,
                    description=doc.description,
                    stock_path=md,
                )
            )
        return sorted(refs, key=lambda r: (r.package, r.name))

    def stock_path(self, package: str, name: str) -> Path | None:
        candidate = self._root / package / f"{name}.md"
        return candidate if candidate.is_file() else None
