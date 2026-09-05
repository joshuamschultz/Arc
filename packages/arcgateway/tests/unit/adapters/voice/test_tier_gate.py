"""T-005 (RED) — voice is federal-forbidden (SPEC-077 COMP-014, REQ-016).

The gate is not a runtime branch inside a handler; it is the AdapterSpec.build()
refusing to construct at the federal tier, plus voice staying out of the
registry's OFFICIAL_ADAPTERS allowlist so the registry blocks it at federal too.
Defense in depth: either alone would stop it.
"""

from __future__ import annotations

import pytest

from arcgateway.adapters.registry import (
    OFFICIAL_ADAPTERS,
    AdapterBuildContext,
    AdapterUnavailableError,
)
from arcgateway.adapters.voice import PLATFORM, build


async def _noop(_event: object) -> None:  # pragma: no cover - never called here
    return None


def _ctx(tier: str) -> AdapterBuildContext:
    return AdapterBuildContext(
        name="voice",
        raw_config={"enabled": True},
        on_message=_noop,
        default_agent_did="did:arc:agent",
        tier=tier,
    )


def test_voice_is_not_an_official_adapter() -> None:
    # Official adapters load at federal; voice must never be one.
    assert "voice" not in OFFICIAL_ADAPTERS


def test_build_refuses_at_federal_tier() -> None:
    with pytest.raises(AdapterUnavailableError):
        build(_ctx("federal"))
    with pytest.raises(AdapterUnavailableError):
        PLATFORM.build(_ctx("federal"))


def test_build_permits_personal_and_enterprise() -> None:
    for tier in ("personal", "enterprise"):
        adapter = build(_ctx(tier))
        assert adapter.name == "voice"
