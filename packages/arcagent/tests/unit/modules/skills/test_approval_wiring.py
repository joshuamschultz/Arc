"""SPEC-044 CRITICAL-2 (arcagent side) — approval provider reuses the shared HumanGate.

The improver's thin ApprovalProvider seam is bound to ``HumanGate.request`` (SPEC-035/043):
a grant → approved; ``None`` (deny/timeout/no-channel) → blocked. configure threads it plus
the agent DID into the improver; a channel-less gate fails closed by construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from arcagent.modules.skills import _runtime
from arcagent.modules.skills.approval import build_skill_approval_provider


class _Gate:
    """Minimal HumanGate stand-in: records the call, returns a preset grant/None."""

    def __init__(self, grant: object | None) -> None:
        self._grant = grant
        self.calls: list[tuple[str, frozenset[str], dict[str, Any]]] = []

    async def request(self, call: Any, *, legs: frozenset[str]) -> object | None:
        self.calls.append((call.tool_name, legs, dict(call.arguments)))
        return self._grant


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.mark.asyncio
async def test_provider_returns_false_when_gate_denies() -> None:
    provider = build_skill_approval_provider(_Gate(None), "did:arc:agent")
    assert await provider("skill.mutation", "s", "detail") is False


@pytest.mark.asyncio
async def test_provider_returns_true_on_grant_and_labels_leg() -> None:
    gate = _Gate(object())  # any non-None grant == approved
    provider = build_skill_approval_provider(gate, "did:arc:agent")
    assert await provider("skill.mutation", "s", "detail") is True
    tool_name, legs, _ = gate.calls[0]
    assert tool_name == "skill.mutation:skill.mutation"
    assert legs == frozenset({"skill_mutation"})  # auditable as a skill-mutation approval


@pytest.mark.asyncio
async def test_provider_fails_closed_on_gate_error() -> None:
    class _Boom:
        async def request(self, call: Any, *, legs: frozenset[str]) -> object | None:
            raise RuntimeError("gate down")

    provider = build_skill_approval_provider(_Boom(), "did:arc:agent")
    assert await provider("skill.mutation", "s", "detail") is False


def test_configure_wires_provider_and_agent_did(tmp_path: Path) -> None:
    """The arcskill adapter gets a real HumanGate-backed provider + the agent DID (federal)."""
    _runtime.configure(
        config={"adapter": "arcskill", "tier": "federal"},
        workspace=tmp_path,
        agent_did="did:arc:test-agent",
        human_gate=_Gate(None),
    )
    adapter = _runtime.state().adapter
    assert adapter._approval_provider is not None  # provider wired
    assert adapter._agent_did == "did:arc:test-agent"
    assert adapter.tier == "federal"


def test_configure_no_gate_leaves_provider_none(tmp_path: Path) -> None:
    """No HumanGate → no provider → the improver fails closed when approval is required."""
    _runtime.configure(config={"adapter": "arcskill", "tier": "federal"}, workspace=tmp_path)
    assert _runtime.state().adapter._approval_provider is None


@pytest.mark.asyncio
async def test_arc_built_call_carries_the_deployments_pii_policy() -> None:
    """D-573 — this call never transits arcllm, so the policy is applied here.

    Without it the gate would present a payload nothing had ever screened, at
    exactly the tier that cares. The policy is arcllm's; only the application
    point is ours.
    """
    gate = _Gate(object())
    with patch("arcllm.registry._resolve_module_config", return_value={"pii_enabled": True}):
        provider = build_skill_approval_provider(gate, "did:arc:agent")
    await provider("retire", "s", "reported by victim@example.com")

    arguments = gate.calls[0][2]
    assert "victim@example.com" not in arguments["detail"]
    assert "[PII:EMAIL]" in arguments["detail"]


@pytest.mark.asyncio
async def test_arc_built_call_is_untouched_when_the_deployment_disables_pii() -> None:
    """The tier decides, not this module — the mirror of the D-572 deletion."""
    gate = _Gate(object())
    with patch("arcllm.registry._resolve_module_config", return_value={"pii_enabled": False}):
        provider = build_skill_approval_provider(gate, "did:arc:agent")
    await provider("retire", "s", "reported by victim@example.com")

    assert gate.calls[0][2]["detail"] == "reported by victim@example.com"
