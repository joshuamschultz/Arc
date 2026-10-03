"""P18-2 §8.5 — a re-auth reaches a running agent with no restart (findings test 7).

Real pieces end to end: a native bundle on disk, the :class:`Connections` façade
an operator surface drives, sealed custody over one arcstore backend, the real
connectors capability attaching for a running agent, and the agent's own tool
registry. The bundle's tool echoes the bearer value its credential handle gave
it, which is what a provider would have received in its header.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcrun import ToolContext
from arcstore.backends.memory import FakeBackend
from arctrust.audit import NullSink
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey
from packages.arcagent.tests.custody_fakes import make_cipher

from arcagent.connections import AuditChain, Connections
from arcagent.connector_control import ConnectorReconcileResult
from arcagent.core.config import ToolConfig, ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tool_registry import ToolRegistry
from arcagent.extension.attachment import ProbeResult, ToolSpec
from arcagent.extension.grants import Connection
from arcagent.modules.connectors import _runtime
from arcagent.modules.connectors.capabilities import Connectors
from arcagent.modules.connectors.install import resolve_secrets
from arcagent.tools.human_gate import HumanGate

AGENT = "slack_agent"
DID = "did:arc:testorg:executor/slack"
CIPHER = make_cipher("reauth")

NATIVE_TOML = """
[extension]
name = "token_echo"
version = "1.0.0"
attachment = "native"
tier_floor = "personal"

[config.native]
entrypoint = "token_echo_entry"

[[secrets]]
name = "user_token"
prompt = "token"

[credential]
bearer = "user_token"

[tools]
allow = ["token_whoami"]

[[tools.declared]]
name = "token_whoami"
description = "Say which token the provider saw."
classification = "read_only"

[approval]
default = "outbound"

[health]
probe = "attachment"
"""

NATIVE_PY = """
from arcagent.extension.attachment import ProbeResult, ToolOutcome, ToolResult, ToolSpec

SPEC = ToolSpec(
    name="token_whoami",
    description="Say which token the provider saw.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    classification="read_only",
)


class TokenEcho:
    def __init__(self, context):
        self._credential = context["credential"]

    def requirements(self):
        return []

    async def probe(self):
        await self._credential.bearer()
        return ProbeResult(reachable=True, detail="ok", tools=[SPEC])

    async def describe_tools(self):
        return [SPEC]

    async def invoke(self, tool, args):
        header = "Bearer " + (await self._credential.bearer()).reveal()
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content=header)


def build_native_attachment(context):
    return TokenEcho(context)
"""

MCP_TOML = """
[extension]
name = "hosted_tools"
version = "1.0.0"
attachment = "mcp"

[config.mcp]
transport = "http"
url = "https://mcp.example.com/mcp"
credential_field = "api_key"
auth_header = "x-api-key"
auth_scheme = ""

[[secrets]]
name = "api_key"
prompt = "key"

[health]
probe = "attachment"
"""


async def _same(backend: FakeBackend) -> FakeBackend:
    return backend


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


class _Control:
    """Records reconcile pushes, the way an in-process agent host would apply them."""

    def __init__(self) -> None:
        self.reconciled: list[str] = []

    async def reconcile(self, agent: str) -> ConnectorReconcileResult:
        self.reconciled.append(agent)
        return ConnectorReconcileResult(status="applied", agent=agent)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.arc_dir = tmp_path / "arc"
        self.root = tmp_path / "bundles"
        self.backend = FakeBackend()
        self.control = _Control()
        self.agent_dir = tmp_path / AGENT
        (self.agent_dir / "workspace").mkdir(parents=True)
        (self.agent_dir / "arcagent.toml").write_text("[agent]\nname='x'\n")
        for name, toml, module in (
            ("token_echo", NATIVE_TOML, NATIVE_PY),
            ("hosted_tools", MCP_TOML, None),
        ):
            bundle = self.root / name
            bundle.mkdir(parents=True)
            (bundle / "extension.toml").write_text(toml)
            if module is not None:
                (bundle / "token_echo_entry.py").write_text(module)

    def connections(self, **kwargs: Any) -> Connections:
        return Connections.for_deployment(
            arc_dir=self.arc_dir,
            extensions_root=self.root,
            audit=AuditChain(),
            state_opener=lambda: _same(self.backend),
            connector_control=self.control,  # type: ignore[arg-type]
            credential_cipher=CIPHER,
            **kwargs,
        )

    async def start_agent(self) -> ToolRegistry:
        gate = HumanGate(
            operator_signer=InProcessSigner(bytes(SigningKey.generate())),
            agent_did=DID,
            tier="personal",
        )
        registry = ToolRegistry(
            config=ToolsConfig(policy=ToolConfig()),
            bus=ModuleBus(),
            telemetry=MagicMock(),
            human_gate=gate,
        )
        identity = MagicMock()
        identity.did = DID
        _runtime.configure(
            config={"extensions_root": str(self.root), "arc_dir": str(self.arc_dir)},
            arcstore_opener=lambda: _same(self.backend),
            workspace=self.agent_dir / "workspace",
            identity=identity,
            config_path=self.agent_dir / "arcagent.toml",
            tool_registry=registry,
            tier="personal",
            human_gate=gate,
            credential_cipher=CIPHER,
        )
        await Connectors().setup(None)
        return registry


async def _call(registry: ToolRegistry, name: str) -> str:
    tools = {tool.name: tool for tool in registry.to_arcrun_tools()}
    assert name in tools, sorted(tools)
    context = ToolContext(
        run_id=str(uuid.uuid4()),
        tool_call_id=str(uuid.uuid4()),
        turn_number=1,
        event_bus=None,
        cancelled=asyncio.Event(),
    )
    return str(await tools[name].execute({}, context))


async def test_reauth_reaches_running_agent_without_restart(tmp_path: Path) -> None:
    world = World(tmp_path)
    connections = world.connections()
    plan = connections.plan("token_echo", "work_slack", agents=[AGENT])
    await connections.install(plan, {"user_token": "xoxp-first"}, agents=[AGENT])
    registry = await world.start_agent()
    tool = next(name for name in (t.name for t in registry.to_arcrun_tools()) if "whoami" in name)
    assert "Bearer xoxp-first" in await _call(registry, tool)

    await world.connections().reauth(plan, {"user_token": "xoxp-second"})

    # The very next call carries the new token: no restart, no reconcile wait.
    assert "Bearer xoxp-second" in await _call(registry, tool)


class _FakeMcp:
    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        return ProbeResult(reachable=True, detail="ok", tools=[])

    async def describe_tools(self) -> list[ToolSpec]:
        return []

    async def invoke(self, tool: str, args: dict[str, Any]) -> Any:
        raise NotImplementedError


async def test_mcp_bundle_reauth_reconciles(tmp_path: Path) -> None:
    world = World(tmp_path)
    connections = world.connections(attachment_factory=lambda *_a, **_k: _FakeMcp())
    connections.registry.define(
        "composio", Connection(extension="hosted_tools", approval="outbound", agents=(AGENT,))
    )
    plan = connections.plan_for("composio")
    await connections.reauth(plan, {"api_key": "key-first"})
    world.control.reconciled.clear()

    await connections.reauth(plan, {"api_key": "key-second"})

    # C7: an MCP attachment resolves its header at session start, so a credential
    # change is pushed to every granted agent, whose rebuild reads the new value.
    assert world.control.reconciled == [AGENT]
    store = await connections._store(NullSink())
    rebuilt = await resolve_secrets(plan.manifest, connection="composio", store=store)
    assert rebuilt["api_key"].reveal() == "key-second"
