"""Integration test — ArcAgent with ArcLLM bridge into the Module Bus.

Verifies:
  1. create_arcllm_bridge() wires TraceRecord events into ModuleBus
  2. ModuleBus handlers receive llm:call_complete, llm:config_change, llm:circuit_change
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.core.agent import ArcAgent, create_arcllm_bridge
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)
from arcagent.core.module_bus import ModuleBus


@pytest.fixture()
def agent_config(tmp_path: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="bridge-test-agent",
            org="testorg",
            type="executor",
            workspace=str(tmp_path / "workspace"),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(
            did="",
            key_dir=str(tmp_path / "keys"),
            vault_path="",
        ),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
    )


class TestArcLLMBridgeIntegration:
    """Full integration: bridge + ModuleBus + event handlers."""

    async def test_bridge_delivers_llm_call_to_bus_handler(self) -> None:
        """TraceRecord with event_type=llm_call → llm:call_complete on bus."""
        bus = ModuleBus()
        received: list[dict[str, Any]] = []

        async def handler(ctx: Any) -> None:
            received.append(ctx.data)

        bus.subscribe("llm:call_complete", handler)

        bridge = create_arcllm_bridge(bus)

        # Simulate 3 LLM calls
        for i in range(3):
            record = MagicMock()
            record.model_dump.return_value = {
                "event_type": "llm_call",
                "trace_id": f"trace-{i}",
                "provider": "anthropic",
                "model": "claude-sonnet-4",
                "duration_ms": 100.0 + i * 50,
                "cost_usd": 0.001 * (i + 1),
                "total_tokens": 500 + i * 100,
            }
            bridge(record)

        # Allow tasks to complete
        for _ in range(10):
            await asyncio.sleep(0)

        assert len(received) == 3
        assert received[0]["trace_id"] == "trace-0"
        assert received[2]["cost_usd"] == 0.003

    async def test_bridge_delivers_mixed_event_types(self) -> None:
        """Multiple event types delivered to correct bus events."""
        bus = ModuleBus()
        llm_calls: list[dict[str, Any]] = []
        config_changes: list[dict[str, Any]] = []
        circuit_changes: list[dict[str, Any]] = []

        async def on_llm(ctx: Any) -> None:
            llm_calls.append(ctx.data)

        async def on_config(ctx: Any) -> None:
            config_changes.append(ctx.data)

        async def on_circuit(ctx: Any) -> None:
            circuit_changes.append(ctx.data)

        bus.subscribe("llm:call_complete", on_llm)
        bus.subscribe("llm:config_change", on_config)
        bus.subscribe("llm:circuit_change", on_circuit)

        bridge = create_arcllm_bridge(bus)

        # LLM call
        bridge({"event_type": "llm_call", "provider": "anthropic", "model": "claude-sonnet-4"})
        # Config change
        bridge({"event_type": "config_change", "event_data": {"actor": "operator"}})
        # Circuit change
        bridge({"event_type": "circuit_change", "event_data": {"new_state": "OPEN"}})
        # Another LLM call
        bridge({"event_type": "llm_call", "provider": "openai", "model": "gpt-4o"})

        for _ in range(10):
            await asyncio.sleep(0)

        assert len(llm_calls) == 2
        assert len(config_changes) == 1
        assert len(circuit_changes) == 1
        assert llm_calls[1]["provider"] == "openai"
        assert circuit_changes[0]["event_data"]["new_state"] == "OPEN"

    async def test_agent_bus_receives_llm_bridge_events(
        self, agent_config: ArcAgentConfig
    ) -> None:
        """Full stack: ArcAgent startup → attach bridge → events flow to bus."""
        agent = ArcAgent(config=agent_config)
        await agent.startup()

        received: list[dict[str, Any]] = []

        async def on_llm_call(ctx: Any) -> None:
            received.append(ctx.data)

        assert agent._bus is not None
        agent._bus.subscribe("llm:call_complete", on_llm_call)

        bridge = create_arcllm_bridge(agent._bus)

        # Simulate LLM call event
        bridge(
            {
                "event_type": "llm_call",
                "trace_id": "test-trace-001",
                "provider": "anthropic",
                "model": "claude-sonnet-4",
                "duration_ms": 250.0,
                "cost_usd": 0.005,
                "total_tokens": 1200,
            }
        )

        for _ in range(10):
            await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0]["trace_id"] == "test-trace-001"

        await agent.shutdown()
