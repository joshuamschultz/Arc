"""RED (SPEC-084 T-1143) — the connector build path speaks the SDK, through the envelope.

SPEC-084 re-bases Arc's MCP client on the official ``mcp`` SDK (:class:`~arcagent.
extension.mcp_attachment.SdkMcpClient`, proven against a real server in
``test_mcp_sdk_client.py``). T-1144 finishes the job: it rewires
:func:`~arcagent.modules.connectors.attachments.build_attachment` so an
``attachment = "mcp"`` connector — http *or* stdio — reaches the agent through the
SDK client instead of the hand-rolled :class:`~arcagent.extension.mcp_attachment.
McpAttachment` + ``HttpTransport`` / ``StdioTransport`` wire.

The governance envelope must not move when the wire does. So this file has two jobs:

* **RED anchor** (fails today, passes after T-1144): the connector build path
  returns an ``SdkMcpClient`` for an mcp manifest. Today it returns an
  ``McpAttachment``, so the feature is *absent* — not an import or collection error,
  since ``SdkMcpClient`` already exists (T-1142). ``build_attachment`` itself is known
  good today (``tests/modules/connectors/test_http_attachment.py`` builds the same
  http manifest and gets an ``McpAttachment`` back), so the only thing failing is the
  swap to the SDK client.
* **Regression guard** (already green with T-1142's ``SdkMcpClient``): an
  ``SdkMcpClient`` bridged through the real :class:`~arcagent.extension.bridge.
  CapabilityBridge` and the real :class:`~arcagent.core.tool_registry.ToolRegistry`
  registers ``echo`` with the *manifest's* classification/tags (never the server's),
  dispatches through the same audited envelope every attachment rides, and leaks no
  ``mcp``-typed object onto the contracts. These are the properties T-1144's deletion
  of the hand-rolled wire must preserve, so they are asserted here where the deletion
  will be judged.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from arcrun.types import ToolContext

from arcagent.core.config import ToolConfig, ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tier import Tier
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport
from arcagent.extension.attachment import ToolOutcome, ToolResult, ToolSpec
from arcagent.extension.bridge import CapabilityBridge
from arcagent.extension.manifest import load_manifest
from arcagent.extension.mcp_attachment import SdkMcpClient
from arcagent.extension.mcp_policy import McpToolPolicy
from arcagent.modules.connectors.attachments import build_attachment

from ._fake_mcp_server import (
    ECHO_TOOL_NAME,
    build_fake_mcp_server,
    connected_session_factory,
)

_SOURCE = "extension:hosted_mcp"

#: A hosted-MCP connector manifest, identical in shape to the one
#: ``tests/modules/connectors/test_http_attachment.py`` builds today (which gets an
#: ``McpAttachment`` back). Only the assertion changes: after T-1144 the same build
#: returns an ``SdkMcpClient``.
_HTTP_MCP_MANIFEST = """
[extension]
name = "hosted_mcp"
version = "1.0.0"
attachment = "mcp"

[tools]
allow = ["remote_read"]

[[tools.declared]]
name = "remote_read"
description = "Read a remote record."
classification = "read_only"

[config.mcp]
transport = "http"
url = "https://mcp.example.com/endpoint"
client_name = "arc-test"

[config.mcp.tools.remote_read]
classification = "read_only"
"""

#: The locally spawned counterpart: same ``attachment = "mcp"``, a stdio transport.
#: Building it never spawns the process, so the RED anchor is as cheap as the http one.
_STDIO_MCP_MANIFEST = """
[extension]
name = "spawned_mcp"
version = "1.0.0"
attachment = "mcp"

[tools]
allow = ["local_read"]

[[tools.declared]]
name = "local_read"
description = "Read a local record."
classification = "read_only"

[config.mcp]
transport = "stdio"
argv = ["mcp-echo-server"]
client_name = "arc-test"

[config.mcp.tools.local_read]
classification = "read_only"
"""


# --- telemetry double, mirrored from test_bridge.py --------------------------


class _NoopSpan:
    def set_attribute(self, *args: Any, **kwargs: Any) -> None: ...

    def add_event(self, *args: Any, **kwargs: Any) -> None: ...

    def record_exception(self, *args: Any, **kwargs: Any) -> None: ...

    def set_status(self, *args: Any, **kwargs: Any) -> None: ...


class _RecordingTelemetry:
    """A real telemetry surface that keeps its events — not a MagicMock."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event_type: str, details: dict[str, Any]) -> None:
        self.events.append((event_type, details))

    @contextlib.asynccontextmanager
    async def tool_span(self, tool_name: str, args: dict[str, Any]) -> AsyncIterator[_NoopSpan]:
        yield _NoopSpan()

    def actions(self) -> list[str]:
        return [event_type for event_type, _ in self.events]


def _registry(telemetry: _RecordingTelemetry) -> ToolRegistry:
    return ToolRegistry(
        config=ToolsConfig(policy=ToolConfig()), bus=ModuleBus(), telemetry=telemetry
    )


def _tool_context() -> ToolContext:
    return ToolContext(
        run_id=str(uuid.uuid4()),
        tool_call_id=str(uuid.uuid4()),
        turn_number=1,
        event_bus=None,
        cancelled=asyncio.Event(),
    )


def _sdk_client_for_echo() -> SdkMcpClient:
    """An SDK client over the in-memory fixture, classified only by the manifest.

    ``echo`` is declared ``read_only`` with a network-read tag here; the fixture
    server advertises no such classification, so a registration that shows these
    values proves they came from the manifest policy and not the server (REQ-269).
    """
    policy = McpToolPolicy(classification="read_only", capability_tags=["network_read"])
    return SdkMcpClient(
        connected_session_factory(build_fake_mcp_server()),
        tools={ECHO_TOOL_NAME: policy},
    )


def _bridge(registry: ToolRegistry, attachment: SdkMcpClient) -> CapabilityBridge:
    return CapabilityBridge(
        registry=registry,
        attachment=attachment,
        transport=ToolTransport.PROCESS,
        source=_SOURCE,
    )


# --- RED anchor: the connector build path returns the SDK client -------------


def test_http_mcp_connector_builds_the_sdk_client(tmp_path: Path) -> None:
    """RED ANCHOR. An ``attachment = "mcp"`` http connector attaches through the SDK.

    Today ``build_attachment`` hands back an ``McpAttachment`` wrapping a
    hand-rolled ``HttpTransport``; this asserts the SDK-backed client instead. It
    fails for the right reason — the connector path does not yet speak the SDK — not
    from a missing symbol, since ``SdkMcpClient`` already exists.
    """
    manifest = load_manifest(_HTTP_MCP_MANIFEST, tier=Tier.PERSONAL)

    attachment = build_attachment(manifest, tmp_path, {})

    assert isinstance(attachment, SdkMcpClient)


def test_stdio_mcp_connector_builds_the_sdk_client(tmp_path: Path) -> None:
    """RED ANCHOR. The spawned counterpart attaches through the SDK too.

    T-1144 rewires both transports, so a stdio manifest must reach the agent as an
    ``SdkMcpClient`` exactly as the http one does. Building it never spawns the
    process, so this fails only because the build still returns an ``McpAttachment``.
    """
    manifest = load_manifest(_STDIO_MCP_MANIFEST, tier=Tier.PERSONAL)

    attachment = build_attachment(manifest, tmp_path, {})

    assert isinstance(attachment, SdkMcpClient)


# --- regression guard: the SDK client rides the unchanged envelope -----------


async def test_manifest_classification_survives_registration() -> None:
    """The manifest's classification and tags reach the registered tool, not the server's.

    ``echo`` is declared ``read_only`` / ``network_read`` in the manifest policy the
    client carries; the fixture server offers no classification. A registration that
    shows those manifest values proves the trifecta-gate inputs come from the manifest
    (REQ-269), which is what T-1144's deletion of the hand-rolled wire must preserve.
    """
    telemetry = _RecordingTelemetry()
    registry = _registry(telemetry)
    client = _sdk_client_for_echo()

    specs = await client.describe_tools()
    _bridge(registry, client).register(specs)

    registered = registry.tools[ECHO_TOOL_NAME]
    assert registered.classification == "read_only"
    assert registered.capability_tags == ["network_read"]
    assert registry.get_classification(ECHO_TOOL_NAME) == "read_only"


async def test_dispatch_rides_the_real_envelope_and_audits() -> None:
    """Dispatching the registered echo reaches the SDK client and emits the envelope audit.

    Registration proves a name exists; only a dispatch through ``to_arcrun_tools`` —
    the same conversion the agent hands the loop — proves the wire is live and the
    audit trail comes for free (REQ-267). The echoed text returning proves the call
    crossed the real protocol to the fixture server and back.
    """
    telemetry = _RecordingTelemetry()
    registry = _registry(telemetry)
    client = _sdk_client_for_echo()

    specs = await client.describe_tools()
    _bridge(registry, client).register(specs)

    tool = {t.name: t for t in registry.to_arcrun_tools()}[ECHO_TOOL_NAME]
    rendered = await tool.execute({"text": "hello"}, _tool_context())

    assert "echo: hello" in rendered
    assert any(action.startswith("tool.") for action in telemetry.actions())


async def test_no_sdk_type_leaks_onto_the_contracts() -> None:
    """No ``mcp``/SDK-shaped object rides the ``ToolSpec`` / ``ToolResult`` / ``RegisteredTool``.

    The SDK is an implementation detail of the wire; the governance envelope sees only
    plain contract values. Every field on the spec, the registered tool, and the invoke
    result is a builtin/enum whose type is defined in ``arcagent`` — never in ``mcp``.
    """
    telemetry = _RecordingTelemetry()
    registry = _registry(telemetry)
    client = _sdk_client_for_echo()

    specs = await client.describe_tools()
    echo_spec = next(spec for spec in specs if spec.name == ECHO_TOOL_NAME)
    _assert_no_mcp_type(echo_spec)
    assert isinstance(echo_spec, ToolSpec)
    assert isinstance(echo_spec.name, str)
    assert isinstance(echo_spec.input_schema, dict)
    assert isinstance(echo_spec.capability_tags, list)

    _bridge(registry, client).register(specs)
    registered = registry.tools[ECHO_TOOL_NAME]
    assert isinstance(registered, RegisteredTool)
    assert isinstance(registered.name, str)
    assert isinstance(registered.input_schema, dict)
    assert isinstance(registered.classification, str)
    _assert_no_mcp_type(registered.name, registered.input_schema, registered.classification)
    for tag in registered.capability_tags:
        assert isinstance(tag, str)

    result = await client.invoke(ECHO_TOOL_NAME, {"text": "hi"})
    assert isinstance(result, ToolResult)
    assert result.outcome is ToolOutcome.OK
    assert isinstance(result.content, str)
    _assert_no_mcp_type(result.content)


def _assert_no_mcp_type(*values: Any) -> None:
    """No value carries a type defined in the ``mcp`` SDK package."""
    for value in values:
        module = type(value).__module__
        assert not module.startswith("mcp"), f"{value!r} is an SDK type from {module}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
