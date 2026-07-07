"""SPEC-033 C1/REQ-011 — reusable load-time re-verification for hub skills.

``verify_artifact_at_load`` re-runs the install-time verify core against the
bytes on disk NOW, so install-time and load-time are independent trust
boundaries (kernel-module / jarsigner precedent).
"""

from __future__ import annotations

import tempfile
import unittest.mock
from pathlib import Path

import pytest
from arcskill.hub.config import HubConfig, RevocationConfig, SkillSource, TierPolicy
from arcskill.hub.errors import SigstoreUnavailable
from arcskill.hub.verify import verify_artifact_at_load


def _bundle(content: bytes = b"fake bundle") -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_load_"))
    p = tmpdir / "skill.tar.gz"
    p.write_bytes(content)
    return p


def _source() -> SkillSource:
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


def test_personal_skips_when_sigstore_unavailable() -> None:
    with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
        result = verify_artifact_at_load(_bundle(), _source(), _personal_config())
    assert result.skipped is True
    # Content hash is recomputed from the bytes on disk at load time.
    assert result.content_hash


def test_federal_floor_holds_at_load_when_sigstore_unavailable() -> None:
    # Federal must not silently skip — the floor is enforced at LOAD, not just install.
    with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
        with pytest.raises(SigstoreUnavailable):
            verify_artifact_at_load(_bundle(), _source(), _federal_config())
