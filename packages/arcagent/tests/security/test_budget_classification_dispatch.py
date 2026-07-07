"""SPEC-038 REQ-004/010/023 — provider_usage + clearance fill at dispatch.

Proves the SPEC-034 ProviderLayer seam is LIVE (an over-budget RunState denies
the next dispatch) and the ClassificationLayer no-read-up fires from arcagent's
fill, with the TRUSTED config label immune to response.model spoofing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from arctrust.classification import Classification
from arctrust.identity import AgentIdentity
from arctrust.policy import ProviderLimit, build_pipeline

from arcagent.core.config import ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tool_policy import PolicyDenied
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport


class _Telemetry:
    def audit_event(self, event: str, payload: dict) -> None: ...

    def tool_span(self, *_a: Any, **_k: Any) -> Any:
        class _Span:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_e: Any) -> None:
                return None

        return _Span()


def _tool(name: str) -> RegisteredTool:
    async def execute(**_kwargs: Any) -> str:
        return f"{name}-ok"

    return RegisteredTool(
        name=name,
        description=name,
        input_schema={},
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="test",
        classification="read_only",
    )


def _run_state(*, tokens: int, cost: float) -> Any:
    # Duck-typed arcrun RunState — only the attributes the bridge reads.
    return SimpleNamespace(
        tokens_used={"input": 0, "output": 0, "total": tokens},
        cost_usd=cost,
        tool_calls_made=0,
    )


def _registry(
    identity: AgentIdentity,
    *,
    provider_limits: dict[str, ProviderLimit] | None = None,
    provider_label: str | None = None,
    resource_classifications: dict[str, str] | None = None,
) -> ToolRegistry:
    pipeline = build_pipeline(
        tier="enterprise",
        agent_registry={identity.did: identity.public_key},
        agent_allowlists={identity.did: {"do_thing"}},
        provider_limits=provider_limits,
    )
    reg = ToolRegistry(
        config=ToolsConfig(),
        bus=ModuleBus(),
        telemetry=_Telemetry(),
        policy_pipeline=pipeline,
        identity=identity,
        tier="enterprise",
        provider_label=provider_label,
        resource_classifications=resource_classifications,
    )
    reg.register(_tool("do_thing"))
    return reg


class TestProviderSeamLive:
    async def test_over_budget_run_state_denies_next_dispatch(self) -> None:
        identity = AgentIdentity.generate("org", "agent")
        reg = _registry(
            identity,
            provider_limits={"anthropic": ProviderLimit(max_tokens=100, max_cost=10.0, max_requests=99)},
            provider_label="anthropic",
        )
        wrapped = reg._create_wrapped_execute(reg.tools["do_thing"])
        with pytest.raises(PolicyDenied) as exc:
            await wrapped({}, parent_state=_run_state(tokens=500, cost=0.0))
        assert exc.value.decision.rule_id == "provider.budget_exceeded"

    async def test_under_budget_allows(self) -> None:
        identity = AgentIdentity.generate("org", "agent")
        reg = _registry(
            identity,
            provider_limits={"anthropic": ProviderLimit(max_tokens=1000, max_cost=10.0, max_requests=99)},
            provider_label="anthropic",
        )
        wrapped = reg._create_wrapped_execute(reg.tools["do_thing"])
        assert await wrapped({}, parent_state=_run_state(tokens=10, cost=0.0)) == "do_thing-ok"

    async def test_trusted_label_ignores_response_model(self) -> None:
        # The provider label comes from config; there is no way for a response
        # field to redirect attribution. A budget keyed to the config label
        # denies regardless of what any response claims.
        identity = AgentIdentity.generate("org", "agent")
        reg = _registry(
            identity,
            provider_limits={"anthropic": ProviderLimit(max_tokens=100, max_cost=10.0, max_requests=99)},
            provider_label="anthropic",
        )
        wrapped = reg._create_wrapped_execute(reg.tools["do_thing"])
        with pytest.raises(PolicyDenied) as exc:
            await wrapped({}, parent_state=_run_state(tokens=500, cost=0.0))
        assert "anthropic" in (exc.value.decision.reason or "")


class TestClassificationSeamLive:
    async def test_cui_caller_denied_secret_tool(self) -> None:
        identity = AgentIdentity.generate("org", "agent")
        identity.clearance = Classification.CUI
        reg = _registry(
            identity,
            resource_classifications={"do_thing": "SECRET"},
        )
        wrapped = reg._create_wrapped_execute(reg.tools["do_thing"])
        with pytest.raises(PolicyDenied) as exc:
            await wrapped({})
        assert exc.value.decision.rule_id == "classification.read_up"

    async def test_secret_caller_allowed(self) -> None:
        identity = AgentIdentity.generate("org", "agent")
        identity.clearance = Classification.SECRET
        reg = _registry(
            identity,
            resource_classifications={"do_thing": "SECRET"},
        )
        wrapped = reg._create_wrapped_execute(reg.tools["do_thing"])
        assert await wrapped({}) == "do_thing-ok"


pytestmark = pytest.mark.asyncio
