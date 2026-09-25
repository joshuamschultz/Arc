"""Durable queue jobs use the broker-issued owner epoch."""

from pathlib import Path

import arcrun
import pytest

from arcagent.core.agent import ArcAgent
from arcagent.core.config import AgentConfig, ArcAgentConfig, IdentityConfig, LLMConfig


class _DurableStore(arcrun.MemoryQueueStore):
    requires_recovery_owner = True
    tenant_scope = "tenant-a"


def _config(tmp_path: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="a", workspace=str(tmp_path)),
        llm=LLMConfig(model="anthropic/claude-opus-5"),
        identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
    )


def test_durable_queue_requires_broker_epoch(tmp_path: Path) -> None:
    queue = arcrun.CallQueueCoordinator(store=_DurableStore(), tenant_scope="tenant-a")
    with pytest.raises(ValueError, match="queue owner epoch"):
        ArcAgent(_config(tmp_path), queue_coordinator=queue, queue_tenant_id="tenant-a")
    for epoch in ("", "0", "01", "-1", "a", "1:other", "1" * 20):
        with pytest.raises(ValueError, match="queue owner epoch"):
            ArcAgent(
                _config(tmp_path),
                queue_coordinator=queue,
                queue_tenant_id="tenant-a",
                queue_owner_epoch=epoch,
            )


def test_durable_queue_uses_broker_epoch(tmp_path: Path) -> None:
    queue = arcrun.CallQueueCoordinator(store=_DurableStore(), tenant_scope="tenant-a")
    agent = ArcAgent(
        _config(tmp_path),
        queue_coordinator=queue,
        queue_tenant_id="tenant-a",
        queue_owner_epoch="42",
    )
    assert agent.queue_owner_epoch == "42"
