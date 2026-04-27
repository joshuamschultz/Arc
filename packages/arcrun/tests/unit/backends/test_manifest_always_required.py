"""Phase C: manifest verification required at ALL tiers (sign pillar).

Before Phase C, ``require_manifest(tier)`` returned False at non-federal tiers,
allowing unsigned entry-point backends to load without signature verification.
This test suite documents the bypass and asserts the hardened behaviour:

  1. Non-federal tier with an unsigned dotted-path backend → BackendSignatureError.
     (RED until require_manifest is changed to always return True.)
  2. Entry-points are now ALWAYS denied regardless of tier (supply-chain lockdown
     per OWASP LLM03 / ASI04); FederalBackendPolicyError is NOT the right error
     at non-federal tiers — instead we raise a new BackendUnsigned or reuse
     BackendSignatureError with a clear message.
  3. Built-ins bypass manifest at all tiers (always trusted).
  4. A non-federal tier caller with a valid signed manifest can still load a
     third-party backend.

Design decision (entry-points): entry-points are permanently disabled at ALL
tiers.  Rationale: setuptools entry-points allow any installed package to inject
a backend without explicit operator review or signing.  The supply chain risk
(ASI04, LLM03) is too high to allow even at personal tier.  Operators who want
third-party backends must supply a signed manifest and use dotted import paths.
This is simpler than "allow-but-verify" because there is no safe place to
obtain the verification key for an arbitrary entry-point package.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import types
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from nacl.signing import SigningKey

from arcrun.backends.loader import (
    BackendSignatureError,
    load_backend,
)

# ---------------------------------------------------------------------------
# Helpers (shared with other backend test files)
# ---------------------------------------------------------------------------


def _canonical(meta: Mapping[str, Any], backends: Sequence[Mapping[str, Any]]) -> bytes:
    return json.dumps(
        {"meta": meta, "backends": backends},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_issuers(trust_dir: Path, did: str, pubkey: bytes) -> None:
    f = trust_dir / "issuers.toml"
    f.write_text(
        f'[issuers."{did}"]\npublic_key = "{base64.b64encode(pubkey).decode()}"\n',
        encoding="utf-8",
    )
    f.chmod(0o600)


def _emit_manifest(
    path: Path,
    *,
    meta: Mapping[str, Any],
    backends: Sequence[Mapping[str, Any]],
    signature_b64: str,
) -> None:
    lines = [
        "[meta]",
        f'issued_at = "{meta["issued_at"]}"',
        f'issuer_did = "{meta["issuer_did"]}"',
        "",
    ]
    for b in backends:
        lines.append("[[backends]]")
        lines.append(f'name = "{b["name"]}"')
        lines.append(f'module = "{b["module"]}"')
        lines.append(f'content_hash = "{b["content_hash"]}"')
        lines.append("")
    lines.append("[signature]")
    lines.append('algorithm = "ed25519"')
    lines.append(f'signature = "{signature_b64}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _local_content_hash() -> str:
    import arcrun.backends.local as local_mod

    raw = Path(local_mod.__file__).read_bytes()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _build_signed_manifest(
    tmp_path: Path,
    issuer_did: str,
    issuer_key: SigningKey,
    backends: Sequence[Mapping[str, Any]],
) -> Path:
    meta = {"issued_at": "2026-04-18T00:00:00Z", "issuer_did": issuer_did}
    sig_b64 = base64.b64encode(
        issuer_key.sign(_canonical(meta, backends)).signature
    ).decode()
    manifest = tmp_path / "allowed_backends.toml"
    _emit_manifest(manifest, meta=meta, backends=backends, signature_b64=sig_b64)
    return manifest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _flush_cache() -> Iterator[None]:
    from arctrust import invalidate_cache

    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture
def issuer_key() -> SigningKey:
    return SigningKey.generate()


@pytest.fixture
def issuer_did() -> str:
    return "did:arc:org:trust-authority/phaseC"


@pytest.fixture
def trust_dir(tmp_path: Path) -> Path:
    d = tmp_path / "trust"
    d.mkdir()
    return d


@pytest.fixture
def trusted_trust_dir(
    trust_dir: Path, issuer_did: str, issuer_key: SigningKey
) -> Path:
    _write_issuers(trust_dir, issuer_did, bytes(issuer_key.verify_key))
    return trust_dir


# ---------------------------------------------------------------------------
# Core bypass tests (RED before fix, GREEN after)
# ---------------------------------------------------------------------------


class TestManifestAlwaysRequired:
    """Manifest is mandatory at every tier — not just federal."""

    def test_enterprise_unsigned_dotted_path_raises(self) -> None:
        """Enterprise tier: loading a dotted-path backend WITHOUT a signed manifest
        must raise BackendSignatureError.

        PRE-PHASE-C: this returns a backend (require_manifest was tier=='federal').
        POST-PHASE-C: raises BackendSignatureError — manifest required at all tiers.
        """
        # FakeBackend satisfies ExecutorBackend Protocol
        from collections.abc import AsyncIterator

        from arcrun.backends.base import BackendCapabilities, ExecHandle

        class FakeEnterprise:
            name = "fake_enterprise"
            capabilities = BackendCapabilities()

            async def run(self, cmd: str, **kw: object) -> ExecHandle:
                return ExecHandle(handle_id="x", backend_name="fake_enterprise")

            async def stream(self, h: ExecHandle) -> AsyncIterator[bytes]:
                yield b""

            async def cancel(self, h: ExecHandle, *, grace: float = 5.0) -> None:
                pass

            async def close(self) -> None:
                pass

        fake_mod = types.ModuleType("_arcrun_fake_enterprise_backend")
        fake_mod.FakeEnterprise = FakeEnterprise  # type: ignore[attr-defined]
        sys.modules["_arcrun_fake_enterprise_backend"] = fake_mod

        try:
            with pytest.raises(BackendSignatureError):
                load_backend(
                    "_arcrun_fake_enterprise_backend:FakeEnterprise",
                    tier="enterprise",
                    manifest_path=None,
                )
        finally:
            del sys.modules["_arcrun_fake_enterprise_backend"]

    def test_personal_unsigned_dotted_path_raises(self) -> None:
        """Personal tier: same as enterprise — manifest required."""
        from collections.abc import AsyncIterator

        from arcrun.backends.base import BackendCapabilities, ExecHandle

        class FakePersonal:
            name = "fake_personal"
            capabilities = BackendCapabilities()

            async def run(self, cmd: str, **kw: object) -> ExecHandle:
                return ExecHandle(handle_id="x", backend_name="fake_personal")

            async def stream(self, h: ExecHandle) -> AsyncIterator[bytes]:
                yield b""

            async def cancel(self, h: ExecHandle, *, grace: float = 5.0) -> None:
                pass

            async def close(self) -> None:
                pass

        fake_mod = types.ModuleType("_arcrun_fake_personal_backend")
        fake_mod.FakePersonal = FakePersonal  # type: ignore[attr-defined]
        sys.modules["_arcrun_fake_personal_backend"] = fake_mod

        try:
            with pytest.raises(BackendSignatureError):
                load_backend(
                    "_arcrun_fake_personal_backend:FakePersonal",
                    tier="personal",
                    manifest_path=None,
                )
        finally:
            del sys.modules["_arcrun_fake_personal_backend"]

    def test_builtin_local_bypasses_manifest_at_enterprise(self) -> None:
        """Built-ins are always trusted and never need a manifest."""
        from arcrun.backends import LocalBackend

        b = load_backend("local", tier="enterprise")
        assert isinstance(b, LocalBackend)

    def test_builtin_local_bypasses_manifest_at_personal(self) -> None:
        """Built-ins are always trusted and never need a manifest."""
        from arcrun.backends import LocalBackend

        b = load_backend("local", tier="personal")
        assert isinstance(b, LocalBackend)


class TestEntryPointsAlwaysDenied:
    """Entry-point discovery is disabled at ALL tiers (supply-chain lockdown).

    Decision: we deny entry-points universally.  There is no safe way to verify
    an arbitrary entry-point package's integrity without a manifest, and the
    complexity of 'allow-but-verify' exceeds the benefit.  Operators who want
    third-party backends must ship a signed manifest.
    """

    def test_entry_points_denied_at_personal(self) -> None:
        """A short alias (not built-in) raises FederalBackendPolicyError at personal tier.

        PRE-PHASE-C: entry_points were tried and eventually ValueError.
        POST-PHASE-C: raises FederalBackendPolicyError — entry-points are
        permanently disabled at ALL tiers (supply-chain lockdown).
        """
        from arcrun.backends.loader import FederalBackendPolicyError

        with pytest.raises(FederalBackendPolicyError):
            load_backend("nonexistent_ep_backend", tier="personal")

    def test_entry_points_denied_at_enterprise(self) -> None:
        from arcrun.backends.loader import FederalBackendPolicyError

        with pytest.raises(FederalBackendPolicyError):
            load_backend("nonexistent_ep_backend", tier="enterprise")

    def test_entry_points_denied_at_federal(self) -> None:
        """Federal always raised FederalBackendPolicyError — unchanged."""
        from arcrun.backends.loader import FederalBackendPolicyError

        with pytest.raises(FederalBackendPolicyError):
            load_backend("nonexistent_ep_backend", tier="federal")


class TestNonFederalWithValidManifest:
    """Non-federal tiers CAN load a third-party backend when they supply a
    valid signed manifest.  The stringency knob is which manifests are trusted;
    the manifest requirement itself is unconditional.
    """

    def test_enterprise_with_signed_manifest_loads(
        self,
        tmp_path: Path,
        trusted_trust_dir: Path,
        issuer_did: str,
        issuer_key: SigningKey,
    ) -> None:
        """Enterprise + valid signed manifest → backend loads successfully."""
        content_hash = _local_content_hash()
        backends = [
            {
                "name": "local_alias",
                "module": "arcrun.backends.local:LocalBackend",
                "content_hash": content_hash,
            }
        ]
        manifest = _build_signed_manifest(tmp_path, issuer_did, issuer_key, backends)

        from arcrun.backends import LocalBackend

        b = load_backend(
            "arcrun.backends.local:LocalBackend",
            tier="enterprise",
            manifest_path=manifest,
            trust_dir=trusted_trust_dir,
        )
        assert isinstance(b, LocalBackend)

    def test_personal_with_signed_manifest_loads(
        self,
        tmp_path: Path,
        trusted_trust_dir: Path,
        issuer_did: str,
        issuer_key: SigningKey,
    ) -> None:
        """Personal + valid signed manifest → backend loads successfully."""
        content_hash = _local_content_hash()
        backends = [
            {
                "name": "local_alias",
                "module": "arcrun.backends.local:LocalBackend",
                "content_hash": content_hash,
            }
        ]
        manifest = _build_signed_manifest(tmp_path, issuer_did, issuer_key, backends)

        from arcrun.backends import LocalBackend

        b = load_backend(
            "arcrun.backends.local:LocalBackend",
            tier="personal",
            manifest_path=manifest,
            trust_dir=trusted_trust_dir,
        )
        assert isinstance(b, LocalBackend)
