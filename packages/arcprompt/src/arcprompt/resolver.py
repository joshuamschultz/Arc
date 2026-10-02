"""PromptResolver — two-layer, first-match prompt resolution (COMP-001).

Resolution checks exactly two layers in order — the agent's overlay, then the
packaged stock — and returns the first match (REQ-122):

* No overlay file present  -> stock, silently (REQ-124). The normal path.
* Overlay present + valid   -> overlay.
* Overlay present + broken  -> raise (REQ-125). Never a silent fall-through to
  stock: a deliberate override must never vanish unnoticed.
* Stock absent when needed  -> :class:`PromptMissing`, a packaging error (REQ-126).

The resolver is constructed once per agent with its overlay root, pinned key,
posture, and catalog, and holds them for the run — never re-deriving posture or
re-reading a key per call (the SPEC-017 build-once/bind precedent).

Overlay layout (COMP-007): ``<overlay_root>/<package>/<name>.md`` with a
detached ``<name>.md.arcsig`` sidecar. ``overlay_root`` is the agent's
``context/`` dir — inside the agent config root, outside the workspace subtree
agent file tools are confined to.
"""

from __future__ import annotations

from pathlib import Path

from arctrust.artifact import ArtifactSignature

from arcprompt.catalog import PromptCatalog, _ensure_safe
from arcprompt.document import PromptDocument, parse_prompt
from arcprompt.errors import PromptMissing, PromptUnsigned
from arcprompt.verifier import SignatureVerifier, TrustPosture

SIGNATURE_SUFFIX = ".arcsig"


class PromptResolver:
    """Resolve a prompt to its effective body, overlay-over-stock, first-match-wins."""

    def __init__(
        self,
        *,
        overlay_root: Path,
        trusted_public_key: bytes | None,
        posture: TrustPosture,
        catalog: PromptCatalog | None = None,
    ) -> None:
        self._overlay_root = overlay_root
        self._posture = posture
        self._verifier = SignatureVerifier(trusted_public_key)
        self._catalog = catalog if catalog is not None else PromptCatalog()

    @property
    def posture(self) -> TrustPosture:
        return self._posture

    def overlay_path(self, package: str, name: str) -> Path:
        """Return the overlay file path for one prompt (may or may not exist)."""
        return self._overlay_root / package / f"{name}.md"

    @property
    def verifier(self) -> SignatureVerifier:
        """The verifier pinned to this resolver's trusted key (for history reads)."""
        return self._verifier

    def resolve(self, package: str, name: str) -> PromptDocument:
        """Resolve ``package:name`` to its effective document.

        ``package``/``name`` are validated as safe path components before any
        filesystem access (SEC-04) — a traversal attempt raises :class:`PromptMissing`
        rather than reading an overlay or stock file outside the roots.
        """
        _ensure_safe(package, name)
        overlay = self.overlay_path(package, name)
        if overlay.is_file():
            return self._load_overlay(overlay)
        return self._load_stock(package, name)

    def _load_stock(self, package: str, name: str) -> PromptDocument:
        stock = self._catalog.stock_path(package, name)
        if stock is None:
            raise PromptMissing(package, name)
        return parse_prompt(stock.read_bytes(), source="stock")

    def resolve_signed_file(self, package: str, name: str, path: Path) -> PromptDocument | None:
        """Verify a signed workspace document (``identity.md``, pinned policy) in place.

        The document stays where the operator and agent expect it; its detached
        signature lives under the overlay root at ``<package>/<name>.md.arcsig`` —
        outside the workspace subtree agent tools reach — and is checked against the
        same pinned operator key as a catalog overlay. Returns ``None`` when the file
        does not exist (nothing to verify). A present file that is unsigned, edited
        after signing, or signed by another key raises :class:`PromptUnsigned`: a
        control-plane document is never used on trust (J2 F2).
        """
        _ensure_safe(package, name)
        if not path.is_file():
            return None
        raw = path.read_bytes()
        sidecar = self.overlay_path(package, name)
        manifest = self._verified_manifest(
            raw, sidecar.with_name(sidecar.name + SIGNATURE_SUFFIX), path
        )
        return PromptDocument(
            name=name,
            description="operator-signed workspace document",
            tunable=False,
            body=raw.decode("utf-8", errors="replace").strip(),
            sha256=manifest.artifact_sha256,
            source="overlay",
            signer_did=manifest.signer_did,
        )

    def _load_overlay(self, path: Path) -> PromptDocument:
        raw = path.read_bytes()
        sig_path = path.with_name(path.name + SIGNATURE_SUFFIX)
        manifest = self._verified_manifest(raw, sig_path, path)
        return parse_prompt(raw, source="overlay", signer_did=manifest.signer_did)

    def _verified_manifest(self, raw: bytes, sig_path: Path, subject: Path) -> ArtifactSignature:
        """Read the sidecar and verify ``raw`` against the pinned key, or raise."""
        if not sig_path.is_file():
            raise PromptUnsigned(f"{subject} has no {SIGNATURE_SUFFIX} signature sidecar")
        try:
            manifest = ArtifactSignature.from_json(sig_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise PromptUnsigned(f"{subject} has an unparseable signature sidecar") from exc
        if not self._verifier.verify(raw, manifest):
            raise PromptUnsigned(f"{subject} failed signature verification against the pinned key")
        return manifest


__all__ = [
    "SIGNATURE_SUFFIX",
    "PromptResolver",
]
