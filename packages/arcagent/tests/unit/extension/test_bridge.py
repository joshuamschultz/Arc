"""CapabilityBridge — attachment tool specs become registered capabilities (COMP-012).

Covers REQ-266 (each allowlisted tool is an individually named capability, so
policy, audit, and approval evaluate the real verb rather than a generic
dispatcher), REQ-267 (the call rides the existing envelope with no stage
bypassed), and REQ-269 (classification and capability tags travel with the tool,
defaulting to the restrictive value).

Every test drives the REAL :class:`~arcagent.core.tool_registry.ToolRegistry`
with a real :class:`~arcagent.core.module_bus.ModuleBus` and a recording
telemetry — nothing about the registry is patched or stubbed. That is the whole
point: a bridge that computes a beautiful list of tools and never registers them
passes any test written against its own return value, and fails every test here.
The same reasoning drives ``test_dispatch_through_the_real_envelope_reaches_the
_attachment``: registration alone proves a name exists, not that calling it
reaches the external system.

[[feedback_producers_unwired_pattern]] is the failure mode this file is shaped
against — correct predicates with dead activating wiring.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
from arcrun.types import ToolContext

from arcagent.core.config import ToolConfig, ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tool_registry import ToolRegistry, ToolTransport

if TYPE_CHECKING:
    from arcagent.extension.attachment import ToolSpec

_SOURCE = "extension:demo"


def _module() -> ModuleType:
    import arcagent.extension.bridge as module

    return module


# --- doubles for what the bridge is NOT under test with ---------------------


class _NoopSpan:
    def set_attribute(self, *args: Any, **kwargs: Any) -> None: ...

    def add_event(self, *args: Any, **kwargs: Any) -> None: ...

    def record_exception(self, *args: Any, **kwargs: Any) -> None: ...

    def set_status(self, *args: Any, **kwargs: Any) -> None: ...


class _RecordingTelemetry:
    """A real telemetry surface that keeps its events — not a MagicMock.

    A mock would answer any attribute and record any call, which is exactly how
    an assertion about a `tool.policy_denied` event can pass while the registry
    emits something else entirely.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event_type: str, details: dict[str, Any]) -> None:
        self.events.append((event_type, details))

    @contextlib.asynccontextmanager
    async def tool_span(self, tool_name: str, args: dict[str, Any]) -> AsyncIterator[_NoopSpan]:
        yield _NoopSpan()

    def actions(self) -> list[str]:
        return [event_type for event_type, _ in self.events]


@dataclass
class _RecordingAttachment:
    """A minimal ExtensionAttachment that records what the envelope delivered."""

    specs: list[Any] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    content: str = "done"

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> Any:
        from arcagent.extension.attachment import ProbeResult

        return ProbeResult(reachable=True, tools=list(self.specs))

    async def describe_tools(self) -> list[Any]:
        return list(self.specs)

    async def invoke(self, tool: str, args: dict[str, Any]) -> Any:
        from arcagent.extension.attachment import ToolResult

        self.calls.append((tool, dict(args)))
        return ToolResult(tool=tool, content=self.content)


# --- fixtures ---------------------------------------------------------------


def _spec(name: str, **overrides: Any) -> ToolSpec:
    from arcagent.extension.attachment import ToolSpec

    base: dict[str, Any] = {
        "name": name,
        "description": f"{name} description",
        "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}}},
    }
    base.update(overrides)
    return ToolSpec(**base)


@pytest.fixture
def telemetry() -> _RecordingTelemetry:
    return _RecordingTelemetry()


def _registry(telemetry: _RecordingTelemetry, policy: ToolConfig | None = None) -> ToolRegistry:
    config = ToolsConfig(policy=policy or ToolConfig())
    return ToolRegistry(config=config, bus=ModuleBus(), telemetry=telemetry)


@pytest.fixture
def registry(telemetry: _RecordingTelemetry) -> ToolRegistry:
    return _registry(telemetry)


@pytest.fixture
def attachment() -> _RecordingAttachment:
    return _RecordingAttachment()


def _bridge(registry: ToolRegistry, attachment: _RecordingAttachment) -> Any:
    module = _module()
    return module.CapabilityBridge(
        registry=registry,
        attachment=attachment,
        transport=ToolTransport.PROCESS,
        source=_SOURCE,
    )


# --- REQ-266: one named capability per tool ---------------------------------


def test_each_tool_registers_under_its_own_name(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    """Policy, audit, and approval must see ``create_issue``, not ``connector_call``."""
    report = _bridge(registry, attachment).register(
        [_spec("create_issue"), _spec("list_issues"), _spec("add_comment")]
    )

    assert sorted(registry.tools) == ["add_comment", "create_issue", "list_issues"]
    assert sorted(report.registered) == ["add_comment", "create_issue", "list_issues"]


def test_no_generic_dispatcher_tool_is_registered(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    """A single passthrough verb would make every policy rule about it meaningless."""
    _bridge(registry, attachment).register([_spec("create_issue"), _spec("list_issues")])

    assert len(registry.tools) == 2


def test_registering_no_tools_registers_nothing_and_does_not_raise(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    """An attachment that serves an empty tool list is a live connection, not an error."""
    report = _bridge(registry, attachment).register([])

    assert registry.tools == {}
    assert report.registered == ()


def test_description_and_input_schema_survive_registration(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    """The schema is what the envelope validates arguments against."""
    spec = _spec("create_issue")

    _bridge(registry, attachment).register([spec])

    registered = registry.tools["create_issue"]
    assert registered.description == spec.description
    assert registered.input_schema == spec.input_schema


def test_the_registered_tool_carries_the_attachment_transport(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    _bridge(registry, attachment).register([_spec("create_issue")])

    assert registry.tools["create_issue"].transport is ToolTransport.PROCESS


def test_the_registered_tool_names_its_source(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    """An operator reading the tool catalog must see which extension supplied a verb."""
    _bridge(registry, attachment).register([_spec("create_issue")])

    assert registry.tools["create_issue"].source == _SOURCE


# --- a denied tool is skipped, not fatal ------------------------------------


def test_a_policy_denied_tool_is_skipped_without_raising(
    telemetry: _RecordingTelemetry, attachment: _RecordingAttachment
) -> None:
    """A least-privilege deny list must not crash an agent at startup."""
    registry = _registry(telemetry, ToolConfig(deny=["delete_issue"]))

    report = _bridge(registry, attachment).register(
        [_spec("create_issue"), _spec("delete_issue"), _spec("list_issues")]
    )

    assert "delete_issue" not in registry.tools
    assert sorted(registry.tools) == ["create_issue", "list_issues"]
    assert report.denied == ("delete_issue",)
    assert sorted(report.registered) == ["create_issue", "list_issues"]


def test_a_denied_tool_leaves_an_audit_trail(
    telemetry: _RecordingTelemetry, attachment: _RecordingAttachment
) -> None:
    """Skipped silently to the agent, never silently to the auditor."""
    registry = _registry(telemetry, ToolConfig(deny=["delete_issue"]))

    _bridge(registry, attachment).register([_spec("create_issue"), _spec("delete_issue")])

    denials = [details for action, details in telemetry.events if action == "tool.policy_denied"]
    assert [details["tool"] for details in denials] == ["delete_issue"]


def test_every_tool_denied_still_returns_cleanly(
    telemetry: _RecordingTelemetry, attachment: _RecordingAttachment
) -> None:
    registry = _registry(telemetry, ToolConfig(deny=["create_issue", "list_issues"]))

    report = _bridge(registry, attachment).register([_spec("create_issue"), _spec("list_issues")])

    assert registry.tools == {}
    assert sorted(report.denied) == ["create_issue", "list_issues"]


# --- REQ-269: classification and capability tags ----------------------------


def test_an_absent_classification_defaults_to_state_modifying(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    """Silence must never buy a tool the parallel-dispatch fast path."""
    _bridge(registry, attachment).register([_spec("create_issue")])

    assert registry.tools["create_issue"].classification == "state_modifying"
    assert registry.get_classification("create_issue") == "state_modifying"


def test_a_declared_read_only_classification_survives_registration(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    _bridge(registry, attachment).register([_spec("list_issues", classification="read_only")])

    assert registry.get_classification("list_issues") == "read_only"


def test_capability_tags_reach_the_registered_tool(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    """These are what ``legs_for_tags`` reads — losing them silently opens the trifecta."""
    tags = ["network_egress", "external_comms"]

    _bridge(registry, attachment).register([_spec("create_issue", capability_tags=tags)])

    assert registry.tools["create_issue"].capability_tags == tags


def test_a_tool_with_no_declared_tags_registers_with_an_empty_tag_list(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    _bridge(registry, attachment).register([_spec("create_issue")])

    assert registry.tools["create_issue"].capability_tags == []


def test_each_tool_keeps_its_own_classification_and_tags(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    """One shared closure over the loop variable would give every tool the last spec's."""
    _bridge(registry, attachment).register(
        [
            _spec("list_issues", classification="read_only", capability_tags=["network_read"]),
            _spec("create_issue", capability_tags=["network_egress"]),
        ]
    )

    assert registry.get_classification("list_issues") == "read_only"
    assert registry.get_classification("create_issue") == "state_modifying"
    assert registry.tools["list_issues"].capability_tags == ["network_read"]
    assert registry.tools["create_issue"].capability_tags == ["network_egress"]


# --- REQ-267: the call really rides the envelope ----------------------------


async def test_dispatch_through_the_real_envelope_reaches_the_attachment(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    """Registration proves a name exists; only a dispatch proves the wire is live.

    Goes through ``to_arcrun_tools`` — the same conversion the agent hands to the
    loop — so argument validation, the bus events, the timeout, and the audit
    emission all run exactly as they will in production.
    """
    attachment.content = "issue #42 created"
    _bridge(registry, attachment).register([_spec("create_issue")])

    tool = {t.name: t for t in registry.to_arcrun_tools()}["create_issue"]
    result = await tool.execute({"subject": "hello"}, _tool_context())

    assert attachment.calls == [("create_issue", {"subject": "hello"})]
    assert "issue #42 created" in result


async def test_each_registered_tool_dispatches_to_its_own_verb(
    registry: ToolRegistry, attachment: _RecordingAttachment
) -> None:
    """Every tool routing to the same upstream name is the late-binding classic."""
    _bridge(registry, attachment).register([_spec("create_issue"), _spec("list_issues")])

    tools = {t.name: t for t in registry.to_arcrun_tools()}
    await tools["list_issues"].execute({"subject": "a"}, _tool_context())
    await tools["create_issue"].execute({"subject": "b"}, _tool_context())

    assert [name for name, _ in attachment.calls] == ["list_issues", "create_issue"]


async def test_a_dispatch_emits_the_envelope_audit_event(
    registry: ToolRegistry, telemetry: _RecordingTelemetry, attachment: _RecordingAttachment
) -> None:
    """Riding the envelope means the audit trail comes for free — assert it arrived."""
    _bridge(registry, attachment).register([_spec("create_issue")])

    tool = {t.name: t for t in registry.to_arcrun_tools()}["create_issue"]
    await tool.execute({"subject": "hello"}, _tool_context())

    assert any(action.startswith("tool.") for action in telemetry.actions())


def _tool_context() -> ToolContext:
    return ToolContext(
        run_id=str(uuid.uuid4()),
        tool_call_id=str(uuid.uuid4()),
        turn_number=1,
        event_bus=None,
        cancelled=asyncio.Event(),
    )
