"""E2E: the workpad hook is actually WIRED to the real bus.

Unit tests prove ``track_runs`` works when called directly. This proves the
activating wiring: a real ``ArcAgent`` with ``[modules.workpad]`` enabled
registers the hook, subscribes it to the bus, and a real ``agent:post_respond``
emission drives the every-N-runs rewrite of ``context.md``. Only the eval model
is stubbed (external dependency).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
    TelemetryConfig,
)
from arcagent.modules.workpad import _runtime


def _config(tmp_path: Path, workspace: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="workpad-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=True),
        context=ContextConfig(max_tokens=10000),
        modules={"workpad": ModuleEntry(enabled=True, config={"every_n_runs": 2})},
    )


def _post_respond_event() -> dict[str, Any]:
    return {
        "result": None,
        "messages": [
            {"role": "user", "content": "pay the vendor invoice by Friday"},
            {"role": "assistant", "content": "noted — I'll track that"},
        ],
        "session_id": "s1",
        "automated": False,
    }


@pytest.mark.asyncio
async def test_post_respond_drives_context_rewrite(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = ArcAgent(config=_config(tmp_path, workspace))
    await agent.startup()

    # Stub the eval model on the module's live runtime state (external dep only).
    model = MagicMock()
    model.invoke = AsyncMock(
        return_value=SimpleNamespace(
            content="Updated: 2026-07-12 | WAITING ON: 1\n\n## WAITING ON\n- vendor invoice (Fri)"
        )
    )
    _runtime.state().eval_model = model

    # Two real bus emissions → every_n_runs=2 fires the rewrite on the 2nd.
    assert agent._bus is not None
    await agent._bus.emit("agent:post_respond", _post_respond_event())
    assert not (workspace / "context.md").exists()  # not yet
    await agent._bus.emit("agent:post_respond", _post_respond_event())

    await asyncio.gather(*list(_runtime.state().background_tasks), return_exceptions=True)

    context = (workspace / "context.md").read_text(encoding="utf-8")
    assert "## WAITING ON" in context
    assert "vendor invoice" in context
    model.invoke.assert_awaited_once()
