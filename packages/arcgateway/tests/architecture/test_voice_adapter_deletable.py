"""T-001 (RED) — the voice platform is deletable and optional (SPEC-077 REQ-004).

A platform is a folder (SPEC-065 REQ-308). The gateway must build with no voice
platform block configured, and voice must not be wired into the registry by name.
Together with the tier gate, keeping voice OUT of OFFICIAL_ADAPTERS is what makes
it both optional and federal-forbidden.
"""

from __future__ import annotations

from arcgateway.adapters import registry


async def _noop(_event: object) -> None:  # pragma: no cover
    return None


def test_gateway_builds_with_no_voice_platform_configured() -> None:
    adapters = registry.build_adapters(
        platforms={},
        on_message=_noop,
        default_agent_did="did:arc:agent",
        tier="personal",
    )
    assert adapters == []


def test_registry_never_names_voice_in_its_allowlist() -> None:
    # Deleting adapters/voice/ can only be lossless if nothing in the registry
    # hard-references it. The official allowlist is the one name list; voice is
    # deliberately absent (also the federal block).
    assert "voice" not in registry.OFFICIAL_ADAPTERS
