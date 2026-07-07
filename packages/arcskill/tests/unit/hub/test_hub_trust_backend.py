"""SPEC-033 #4 — HubTrustBackend wires verify_artifact_at_load behind the seam.

Previously ``verify_artifact_at_load`` was referenced only by its own test.
:class:`HubTrustBackend` adapts it to the loader's structural ``TrustBackend``
Protocol (``verify(artifact, content, *, trusted_public_key=None) -> bool``),
so hub skills get load-time integrity checking and the function is production-
referenced. Fail-closed on any verification error.
"""

from __future__ import annotations

import tempfile
import unittest.mock
from pathlib import Path

from arcskill.hub.config import HubConfig, RevocationConfig, SkillSource, TierPolicy
from arcskill.hub.trust_backend import HubTrustBackend


def _bundle(content: bytes = b"fake bundle") -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_backend_"))
    p = tmpdir / "skill.tar.gz"
    p.write_bytes(content)
    return p


def _source(_artifact: Path) -> SkillSource:
    return SkillSource(name="arc-official", type="github", repo="arc-foundation/skills")


def _personal_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="personal"),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json", fail_closed_if_unreachable=False
        ),
    )


def _federal_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="federal"),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json", fail_closed_if_unreachable=True
        ),
    )


def test_matches_trust_backend_protocol_shape() -> None:
    backend = HubTrustBackend(_personal_config(), _source)
    # Structural conformance to the loader's TrustBackend Protocol.
    assert callable(backend.verify)


def test_personal_skip_is_allowed_at_load() -> None:
    backend = HubTrustBackend(_personal_config(), _source)
    bundle = _bundle()
    with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
        assert backend.verify(bundle, bundle.read_bytes()) is True


def test_federal_floor_fails_closed_when_sigstore_unavailable() -> None:
    backend = HubTrustBackend(_federal_config(), _source)
    bundle = _bundle()
    # Federal must not silently skip; verify_artifact_at_load raises → deny.
    with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
        assert backend.verify(bundle, bundle.read_bytes()) is False
