"""SPEC-083 COMP-023 — the fleet's shared-knowledge port reaches memory on a real agent.

The seam under test is the public, module-agnostic one: ``ArcAgent.attach_shared_knowledge``
publishes ``knowledge:shared_attached`` on the agent's own module bus, and the
memory module (installed as a real signed bundle) subscribes and holds the port on
its per-DID runtime state. Core never names the memory module.

Proves: attach -> memory holds the port; detach -> it is gone; an agent with no
memory module accepts attach/detach and is otherwise unaffected; a not-yet-started
agent refuses. Only the model loader is stubbed.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import arcbundle
import pytest
from arctrust import ValidatorsConfig, generate_keypair
from arctrust.paths import identity_dir, module_root, operator_dir

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
    SecurityConfig,
    TelemetryConfig,
)
from arcagent.core.errors import ExtensionError

_SOURCE_CATALOG = Path(__file__).resolve().parents[2] / "src" / "arcagent" / "modules"
_ISSUER = "did:arc:test-operator"
_ISSUER_KEYPAIR = generate_keypair()


class _Port:
    """A structural ``SharedKnowledgePort``; identity is all this test inspects."""

    async def save(self, draft: Any, access: Any) -> Any:  # pragma: no cover
        raise PermissionError

    async def read(self, reference: str, access: Any) -> Any:  # pragma: no cover
        raise LookupError

    async def search(self, query: str, access: Any) -> list[Any]:  # pragma: no cover
        return []

    async def promote(self, source: Any, access: Any, **_: Any) -> Any:  # pragma: no cover
        raise PermissionError

    async def revoke(self, reference: str, access: Any) -> None:  # pragma: no cover
        return None


def _install_memory(arc_home: Path, agent_dir: Path, tmp_path: Path) -> None:
    bundle = arcbundle.build_bundle(
        _SOURCE_CATALOG / "memory",
        module="memory",
        version="1.0.0",
        private_key=_ISSUER_KEYPAIR.private_key,
        issuer=_ISSUER,
        out=tmp_path / "bundles" / "memory",
    )
    verified = arcbundle.verify_bundle(
        bundle, tier="personal", trusted_issuers={_ISSUER: _ISSUER_KEYPAIR.public_key}
    )
    installed = arcbundle.materialize(verified, module_root(arc_home))
    arcbundle.copy_capabilities(installed, agent_dir, module="memory")


def _config(arc_home: Path, workspace: Path, *, with_memory: bool) -> ArcAgentConfig:
    modules = (
        {
            "memory": ModuleEntry(
                enabled=True, config={"brain": "arcmemory", "embed_backend": "none"}
            )
        }
        if with_memory
        else {}
    )
    return ArcAgentConfig(
        agent=AgentConfig(
            name="attach-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(key_dir=str(identity_dir(arc_home))),
        security=SecurityConfig(
            operator_key_dir=str(operator_dir(arc_home)),
            validators=ValidatorsConfig(trusted_keys=(_ISSUER_KEYPAIR.public_key.hex(),)),
        ),
        telemetry=TelemetryConfig(enabled=True, export_traces=False),
        modules=modules,
    )


def _agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_memory: bool) -> ArcAgent:
    arc_home, agent_dir, workspace = tmp_path / "arc", tmp_path / "agent", tmp_path / "ws"
    for path in (arc_home, agent_dir, workspace):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_home))
    if with_memory:
        _install_memory(arc_home, agent_dir, tmp_path)
    return ArcAgent(
        config=_config(arc_home, workspace, with_memory=with_memory),
        config_path=agent_dir / "arcagent.toml",
    )


@asynccontextmanager
async def _started(agent: ArcAgent) -> AsyncIterator[ArcAgent]:
    model = MagicMock()
    model.close = AsyncMock()
    with (
        patch("arcagent.core.model_manager.load_eval_model", return_value=model),
        patch("arcagent.utils.model_helpers.load_eval_model", return_value=model),
    ):
        await agent.startup()
        try:
            yield agent
        finally:
            await agent.shutdown()


def _memory_state(agent: ArcAgent) -> Any:
    runtime = sys.modules.get("arcagent.modules.memory._runtime")
    assert runtime is not None, "memory runtime never loaded — module did not install"
    return runtime.state_for(agent.did)


async def test_attach_hands_the_port_to_memory_and_detach_takes_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _Port()
    async with _started(_agent(tmp_path, monkeypatch, with_memory=True)) as agent:
        assert _memory_state(agent).shared_knowledge is None

        await agent.attach_shared_knowledge(port)
        assert _memory_state(agent).shared_knowledge is port

        await agent.detach_shared_knowledge()
        assert _memory_state(agent).shared_knowledge is None


async def test_reattach_replaces_the_previous_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _Port(), _Port()
    async with _started(_agent(tmp_path, monkeypatch, with_memory=True)) as agent:
        await agent.attach_shared_knowledge(first)
        await agent.attach_shared_knowledge(second)

        assert _memory_state(agent).shared_knowledge is second


async def test_agent_without_memory_accepts_attach_and_detach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _started(_agent(tmp_path, monkeypatch, with_memory=False)) as agent:
        tools_before = {tool.name for tool in agent.registered_tools}

        await agent.attach_shared_knowledge(_Port())
        await agent.detach_shared_knowledge()

        assert {tool.name for tool in agent.registered_tools} == tools_before


async def test_attach_before_start_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent(tmp_path, monkeypatch, with_memory=False)

    with pytest.raises(ExtensionError, match="started"):
        await agent.attach_shared_knowledge(_Port())
