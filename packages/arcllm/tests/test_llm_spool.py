"""arcllm → arcstore.spool auto-instrumentation (SPEC-026 FR-4).

Every completion records one ``llm_call`` spool line by default — on success
AND on error (finally-guarded, closes defect C3). Recording is gated by
``arcstore_enabled`` (default true) and imports only ``arcstore.spool`` —
never the store/backends (module boundary, AC-4.3).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import arcllm.modules.telemetry as telemetry_mod
from arcllm.modules.telemetry import TelemetryModule
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
    model="test-model",
    stop_reason="end_turn",
)


def _inner() -> MagicMock:
    inner = MagicMock(spec=LLMProvider)
    inner.name = "test-provider"
    inner.model_name = "test-model"
    inner.invoke = AsyncMock(return_value=_OK)
    return inner


def _config(**overrides: object) -> dict:
    base = {
        "cost_input_per_1m": 3.0,
        "cost_output_per_1m": 15.0,
        "agent_did": "did:arc:acme:analyst/aabbccdd",
    }
    base.update(overrides)
    return base


@pytest.fixture
def messages() -> list[Message]:
    return [Message(role="user", content="hi")]


async def test_completion_records_llm_call(messages: list[Message]) -> None:
    module = TelemetryModule(_config(), _inner())
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        await module.invoke(messages)
    assert len(recorded) == 1
    rec = recorded[0]
    assert rec.kind == "llm_call"
    assert rec.actor_did == "did:arc:acme:analyst/aabbccdd"
    assert rec.outcome == "ok"
    assert rec.model == "test-model"
    assert rec.prompt_tokens == 100
    assert rec.completion_tokens == 50
    assert rec.cost_usd is not None and rec.cost_usd > 0
    assert rec.latency_ms is not None


async def test_error_call_records_outcome_error(messages: list[Message]) -> None:
    inner = _inner()
    inner.invoke = AsyncMock(side_effect=RuntimeError("boom"))
    module = TelemetryModule(_config(), inner)
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        with pytest.raises(RuntimeError):
            await module.invoke(messages)
    assert len(recorded) == 1
    assert recorded[0].outcome == "error"


async def test_disabled_records_nothing(messages: list[Message]) -> None:
    module = TelemetryModule(_config(arcstore_enabled=False), _inner())
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        await module.invoke(messages)
    assert recorded == []
