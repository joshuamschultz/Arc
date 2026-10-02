"""P12 — adding an MCP server end to end, through the façade every surface uses.

Real everything between the operator and the agent: a real stdio MCP server child process,
the real ``Connections`` façade, a real signed bundle on disk, the real connectors
capability, and a real ``ToolRegistry`` looked at afterwards. The two properties that matter
are the ones a unit test of the spec cannot see: the operator's chosen tools (and only those)
reach the agent under namespaced names, and nothing about the credential survives anywhere
it should not.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from arctrust.paths import config_file
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

from arcagent.capabilities import artifact_signing
from arcagent.connections import AuditChain, Connections
from arcagent.core.config import ToolConfig, ToolsConfig
from arcagent.core.errors import ExtensionError
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tool_registry import ToolRegistry
from arcagent.extension.grants import ConnectionRegistry
from arcagent.modules.connectors import _runtime
from arcagent.modules.connectors.capabilities import Connectors
from arcagent.modules.connectors.mcp_bundle import McpServerSpec, McpToolChoice
from arcagent.tools.human_gate import HumanGate

_SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_stdio_server.py"
_AGENT = "serving_agent"
_DID = "did:arc:testorg:executor/serving"
_SECRET = "s3cr3t-VALUE-9f2c41"


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


async def _open(backend: FakeBackend) -> FakeBackend:
    return backend


class _World:
    def __init__(self, tmp_path: Path) -> None:
        self.arc_dir = tmp_path / "arc"
        self.arc_dir.mkdir()
        self.root = tmp_path / "extensions"
        self.data_dir = tmp_path / "data"
        self.sink = _Sink()
        self.backend = FakeBackend()
        self.agent_dir = tmp_path / _AGENT
        workspace = self.agent_dir / "workspace"
        workspace.mkdir(parents=True)
        (self.agent_dir / "arcagent.toml").write_text(
            "[agent]\n"
            'name = "serving-agent"\n'
            'org = "testorg"\n'
            'type = "executor"\n'
            f'workspace = "{workspace}"\n\n'
            '[llm]\nmodel = "test/model"\n\n'
            f'[identity]\ndid = "{_DID}"\n\n'
            '[security]\ntier = "personal"\n',
            encoding="utf-8",
        )

    def connections(self) -> Connections:
        return Connections.for_deployment(
            arc_dir=self.arc_dir,
            data_dir=self.data_dir,
            extensions_root=self.root,
            audit=AuditChain.held(self.sink),
            state_opener=lambda: _open(self.backend),
        )

    async def start_agent(self) -> ToolRegistry:
        gate = HumanGate(
            operator_signer=InProcessSigner(bytes(SigningKey.generate())),
            agent_did=_DID,
            tier="personal",
        )
        registry = ToolRegistry(
            config=ToolsConfig(policy=ToolConfig()),
            bus=ModuleBus(),
            telemetry=MagicMock(),
            human_gate=gate,
        )
        identity: Any = MagicMock()
        identity.did = _DID
        _runtime.configure(
            config={
                "data_dir": str(self.data_dir),
                "extensions_root": str(self.root),
                "arc_dir": str(self.arc_dir),
            },
            telemetry=None,
            arcstore_opener=lambda: _open(self.backend),
            workspace=self.agent_dir / "workspace",
            identity=identity,
            config_path=self.agent_dir / "arcagent.toml",
            tool_registry=registry,
            tier="personal",
            human_gate=gate,
        )
        await Connectors().setup(None)
        return registry


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[_World]:
    yield _World(tmp_path)


def _spec(**overrides: Any) -> McpServerSpec:
    fields: dict[str, Any] = {
        "name": "fixture",
        "display": "Fixture server",
        "description": "A fixture MCP server.",
        "transport": "stdio",
        "argv": [sys.executable, str(_SERVER)],
        "tools": {
            "echo": McpToolChoice(classification="read_only"),
            "bash": McpToolChoice(),
        },
    }
    fields.update(overrides)
    return McpServerSpec(**fields)


async def test_preview_lists_every_advertised_tool_and_writes_nothing(world: _World) -> None:
    found = await world.connections().preview_mcp_server(_spec(tools={}), secret_values={})

    assert {tool.name for tool in found} >= {"echo", "danger_delete", "bash", "write"}
    assert not world.root.exists()
    assert "mcp_server.preview" in {event.action for event in world.sink.events}


async def test_fake_stdio_server_registers_declared_tools_only(world: _World) -> None:
    added = await world.connections().add_mcp_server(_spec(), agents=[_AGENT], secret_values={})

    assert set(added.report.tools) >= {"fixture__echo", "fixture__bash"}
    registry = await world.start_agent()

    assert {"fixture__echo", "fixture__bash"} <= set(registry.tools)
    # The operator did not choose these, so the agent cannot call them, whatever the
    # server advertises and however it describes itself.
    assert "fixture__danger_delete" not in registry.tools
    assert "fixture__write" not in registry.tools
    # The server's own names never reach the registry bare, so none can shadow a built-in.
    assert not {"bash", "write", "danger_delete", "echo"} & set(registry.tools)
    # Classification is the operator's: echo was declared read_only, bash took the default.
    assert registry.tools["fixture__echo"].classification == "read_only"
    assert registry.tools["fixture__bash"].classification == "state_modifying"


async def test_a_chosen_tool_is_callable_under_its_namespaced_name(world: _World) -> None:
    await world.connections().add_mcp_server(_spec(), agents=[_AGENT], secret_values={})
    registry = await world.start_agent()

    result = await registry.tools["fixture__echo"].execute(text="hello")

    assert "echo: hello" in str(result)


async def test_lying_annotations_are_ignored(world: _World) -> None:
    """The server calls its destructive tool read-only; only the operator's word counts."""
    spec = _spec(tools={"danger_delete": McpToolChoice()})
    await world.connections().add_mcp_server(spec, agents=[_AGENT], secret_values={})
    registry = await world.start_agent()

    assert registry.tools["fixture__danger_delete"].classification == "state_modifying"


async def test_the_generated_bundle_is_signed_and_grants_are_recorded(world: _World) -> None:
    added = await world.connections().add_mcp_server(_spec(), agents=[_AGENT], secret_values={})

    files = [p for p in added.bundle.rglob("*") if p.is_file() and p.suffix != ".arcsig"]
    assert files
    assert all(artifact_signing.load_signature(path) is not None for path in files)
    assert ConnectionRegistry(world.arc_dir).get("fixture").agents == (_AGENT,)


async def test_a_bundle_edited_after_install_never_attaches(world: _World) -> None:
    added = await world.connections().add_mcp_server(_spec(), agents=[_AGENT], secret_values={})
    manifest = added.bundle / "extension.toml"
    manifest.write_text(manifest.read_text().replace('"fixture__echo"', '"fixture__write"'))

    registry = await world.start_agent()

    assert not any(name.startswith("fixture__") for name in registry.tools)


async def test_a_second_add_under_the_same_name_is_refused(world: _World) -> None:
    connections = world.connections()
    await connections.add_mcp_server(_spec(), agents=[_AGENT], secret_values={})

    with pytest.raises(ExtensionError):
        await connections.add_mcp_server(_spec(), agents=[_AGENT], secret_values={})


async def test_name_collision_with_an_existing_bundle_is_refused(world: _World) -> None:
    (world.root / "fixture").mkdir(parents=True)

    with pytest.raises(ExtensionError):
        await world.connections().add_mcp_server(_spec(), agents=[_AGENT], secret_values={})


async def test_a_server_that_will_not_start_leaves_nothing_behind(world: _World) -> None:
    broken = tmp_script(world, "import sys\nsys.exit(3)\n")
    spec = _spec(argv=[sys.executable, str(broken)])

    with pytest.raises(ExtensionError):
        await world.connections().add_mcp_server(spec, agents=[_AGENT], secret_values={})

    assert not (world.root / "fixture").exists()
    assert "fixture" not in ConnectionRegistry(world.arc_dir).all()


def tmp_script(world: _World, body: str) -> Path:
    path = world.arc_dir / "broken_server.py"
    path.write_text(body, encoding="utf-8")
    return path


async def test_credential_never_appears_in_bundle_audit_or_report(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    spec = _spec(env_refs={"api_key": "FIXTURE_API_KEY"})
    caplog.set_level("DEBUG")

    added = await world.connections().add_mcp_server(
        spec, agents=[_AGENT], secret_values={"api_key": _SECRET}
    )

    assert _SECRET not in repr(added)
    assert all(
        _SECRET not in path.read_text() for path in added.bundle.rglob("*") if path.is_file()
    )
    assert all(_SECRET not in repr(event) for event in world.sink.events)
    assert _SECRET not in caplog.text
    assert _SECRET not in " ".join(spec.argv)


async def test_every_step_is_audited_with_the_spec_digest(world: _World) -> None:
    added = await world.connections().add_mcp_server(_spec(), agents=[_AGENT], secret_values={})

    by_action = {event.action: event for event in world.sink.events}
    for action in (
        "mcp_server.spec_validated",
        "mcp_server.bundle_written",
        "mcp_server.bundle_signed",
        "mcp_server.add",
    ):
        assert action in by_action, action
        assert by_action[action].extra["spec_sha256"] == added.spec_sha256


async def test_a_refusal_is_audited_and_writes_nothing(world: _World) -> None:
    spec = _spec(argv=["./relative-server"])

    with pytest.raises(ExtensionError):
        await world.connections().add_mcp_server(spec, agents=[_AGENT], secret_values={})

    denied = [e for e in world.sink.events if e.action == "mcp_server.add" and e.outcome == "deny"]
    assert denied
    assert not world.root.exists()


async def test_enterprise_refuses_an_unlisted_program_before_writing(world: _World) -> None:
    config = config_file("arcagent.toml", world.arc_dir)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[security]\ntier = "enterprise"\n', encoding="utf-8")

    with pytest.raises(ExtensionError) as caught:
        await world.connections().add_mcp_server(_spec(), agents=[], secret_values={})

    assert "allowlist" in caught.value.message
    assert not world.root.exists()


async def test_federal_refuses_every_operator_added_server(world: _World) -> None:
    config = config_file("arcagent.toml", world.arc_dir)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[security]\ntier = "federal"\n', encoding="utf-8")

    with pytest.raises(ExtensionError) as caught:
        await world.connections().add_mcp_server(_spec(), agents=[], secret_values={})

    assert "federal" in caught.value.message
    assert not world.root.exists()


async def test_a_removed_server_can_be_added_again(world: _World) -> None:
    await world.connections().add_mcp_server(_spec(), agents=[_AGENT], secret_values={})
    await world.connections().remove("fixture")
    assert "fixture" not in ConnectionRegistry(world.arc_dir).all()

    again = await world.connections().add_mcp_server(_spec(), agents=[_AGENT], secret_values={})

    assert again.report.instance == "fixture"


async def test_a_hand_written_bundle_is_never_overwritten_by_an_add(world: _World) -> None:
    mine = world.root / "fixture"
    mine.mkdir(parents=True)
    (mine / "extension.toml").write_text("# my own bundle\n", encoding="utf-8")

    with pytest.raises(ExtensionError):
        await world.connections().add_mcp_server(_spec(), agents=[_AGENT], secret_values={})

    assert (mine / "extension.toml").read_text(encoding="utf-8") == "# my own bundle\n"


async def test_the_card_says_honestly_that_knowledge_does_not_apply(world: _World) -> None:
    await world.connections().add_mcp_server(_spec(), agents=[_AGENT], secret_values={})

    entry = next(e for e in world.connections().catalog() if e.name == "fixture")

    assert entry.knowledge_mode == "non_indexable"
    assert "tools only" in entry.knowledge_reason


async def test_missing_or_extra_credential_fields_are_refused_by_name(world: _World) -> None:
    spec = _spec(env_refs={"api_key": "FIXTURE_API_KEY"})

    with pytest.raises(ExtensionError) as missing:
        await world.connections().add_mcp_server(spec, agents=[_AGENT], secret_values={})
    with pytest.raises(ExtensionError) as extra:
        await world.connections().add_mcp_server(
            _spec(), agents=[_AGENT], secret_values={"surprise": _SECRET}
        )

    assert "api_key" in missing.value.message
    assert _SECRET not in extra.value.message
    assert not world.root.exists()
