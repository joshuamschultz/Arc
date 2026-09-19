"""RED (SPEC-084 T-1141) — the SDK-backed MCP client, proven against a real server.

SPEC-084 re-bases Arc's MCP client on the official ``mcp`` SDK so it negotiates
the real protocol (the Legacy ``initialize`` handshake the ecosystem runs) instead
of only the hand-rolled ``2026-07-28`` ``server/discover`` dialect. This test is the
CON-15 contract test for that client: it drives the intended ``SdkMcpClient``
against a *real* ``mcp``-SDK server (``_fake_mcp_server``) over the SDK's in-memory
transport and asserts the three hook methods work end-to-end.

It fails today because ``SdkMcpClient`` does not exist — the current attachment
(:class:`~arcagent.extension.mcp_attachment.McpAttachment`) only speaks the
``server/discover`` dialect and cannot handshake with a standard ``initialize``
server, so there is nothing here that can talk to the fixture. The failure is the
feature's absence, not a fixture or transport error: ``_fake_mcp_server`` is
independently exercised by :func:`test_fixture_server_answers_over_the_real_sdk`,
which passes today.

Every assertion is written against the unchanged ``ExtensionAttachment`` contract
(``probe`` / ``describe_tools`` / ``invoke`` + their value types), never against an
MCP- or SDK-shaped one: SDK types must not leak past the client boundary.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ListToolsResult, Tool

from arcagent.extension.attachment import (
    ExtensionAttachment,
    ProbeResult,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)

from ._fake_mcp_server import (
    ECHO_TOOL_NAME,
    build_fake_mcp_server,
    connected_session_factory,
)


async def test_fixture_server_answers_over_the_real_sdk() -> None:
    """Guard: the fixture is a working SDK server, so a later RED is about the client.

    If this ever fails, the RED below is lying — the problem would be the fixture,
    not the missing ``SdkMcpClient``.
    """
    async with connected_session_factory()() as session:
        init = await session.initialize()
        assert init.serverInfo.name == "fake-arc-mcp"
        listed = await session.list_tools()
        assert ECHO_TOOL_NAME in {tool.name for tool in listed.tools}
        result = await session.call_tool(ECHO_TOOL_NAME, {"text": "hi"})
        assert result.isError is False
        assert "hi" in result.content[0].text  # type: ignore[union-attr]


def _build_client() -> ExtensionAttachment:
    """Construct the intended SDK-backed client against the in-memory fixture.

    Imported lazily so this file's other test (the fixture guard) still collects
    and runs while ``SdkMcpClient`` does not yet exist. The intended constructor
    takes an injected session factory — the same seam production fills with an
    HTTP or stdio SDK session — so a conformance test needs no socket.
    """
    from arcagent.extension.mcp_attachment import SdkMcpClient

    return SdkMcpClient(connected_session_factory(build_fake_mcp_server()))


async def test_sdk_client_satisfies_the_extension_attachment_seam() -> None:
    """The new client plugs into the unchanged hook, like every other attachment."""
    client = _build_client()
    assert isinstance(client, ExtensionAttachment)


async def test_probe_reports_reachable_with_the_servers_real_tool() -> None:
    """``probe`` proves the connection and reports the live tool list from the SDK."""
    client = _build_client()

    probe = await client.probe()

    assert isinstance(probe, ProbeResult)
    assert probe.reachable is True
    assert ECHO_TOOL_NAME in {tool.name for tool in probe.tools}


async def test_describe_tools_lists_echo_with_its_input_schema() -> None:
    """``describe_tools`` surfaces ``echo`` and the SDK-generated input schema."""
    client = _build_client()

    specs = await client.describe_tools()

    assert all(isinstance(spec, ToolSpec) for spec in specs)
    echo = next(spec for spec in specs if spec.name == ECHO_TOOL_NAME)
    assert "text" in echo.input_schema.get("properties", {})


async def test_invoke_returns_the_echoed_result_over_the_sdk() -> None:
    """``invoke`` calls the tool over the real protocol and returns its output."""
    client = _build_client()

    result = await client.invoke(ECHO_TOOL_NAME, {"text": "hi"})

    assert isinstance(result, ToolResult)
    assert result.tool == ECHO_TOOL_NAME
    assert result.outcome is ToolOutcome.OK
    assert "hi" in result.content


# --- SECURITY / ERROR branches: the client must degrade, never leak or mask ----


class _ListToolsRaisingSession:
    """A session whose ``list_tools`` blows up, standing in for a transport fault."""

    def __init__(self, message: str) -> None:
        self._message = message

    async def __aenter__(self) -> _ListToolsRaisingSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def list_tools(self) -> ListToolsResult:
        raise RuntimeError(self._message)


async def test_probe_returns_unreachable_when_the_session_fails() -> None:
    """probe() is the loader's reachability answer, so it must degrade, not raise.

    Pins the fail-safe at lines ~239-242: a session whose ``list_tools`` raises must
    surface as ``ProbeResult(reachable=False)`` carrying the error text — never an
    exception escaping the one method the loader trusts to always answer.
    """
    from arcagent.extension.mcp_attachment import SdkMcpClient

    boom = "transport dropped mid-handshake"
    # Partial fake session: only the methods probe() exercises are implemented.
    client = SdkMcpClient(lambda: _ListToolsRaisingSession(boom))  # type: ignore[arg-type, return-value]

    probe = await client.probe()

    assert isinstance(probe, ProbeResult)
    assert probe.reachable is False
    assert boom in (probe.detail or "")


def _build_erroring_server() -> FastMCP:
    """A real SDK server whose single tool always raises, so its result isError."""
    server = FastMCP(name="fake-arc-mcp")

    @server.tool(description="A tool that always fails.")
    def explode(text: str) -> str:
        raise RuntimeError("tool blew up on purpose")

    return server


async def test_invoke_surfaces_a_server_error_as_error_outcome() -> None:
    """A server-reported failure is surfaced as ERROR, not masked as OK.

    Pins line ~269-273: when the SDK result carries ``isError=True`` the client must
    return ``ToolOutcome.ERROR`` with the server's own content, so the caller sees the
    real failure rather than a false success.
    """
    from arcagent.extension.mcp_attachment import SdkMcpClient

    client = SdkMcpClient(connected_session_factory(_build_erroring_server()))

    result = await client.invoke("explode", {"text": "hi"})

    assert isinstance(result, ToolResult)
    assert result.outcome is ToolOutcome.ERROR
    assert "blew up on purpose" in result.content


class _TwoToolSession:
    """A session serving one clean tool and one that asks for header mirroring."""

    async def __aenter__(self) -> _TwoToolSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def list_tools(self) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(
                    name="clean",
                    description="a normal tool",
                    inputSchema={
                        "type": "object",
                        "properties": {"a": {"type": "string"}},
                    },
                ),
                Tool(
                    name="mirrored",
                    description="a tool that wants a header mirror",
                    inputSchema={
                        "type": "object",
                        "properties": {"key": {"type": "string", "x-mcp-header": "X-Api-Key"}},
                    },
                ),
            ]
        )


async def test_describe_tools_excludes_a_header_mirroring_tool() -> None:
    """A tool that needs a value mirrored into an HTTP header is dropped, not called.

    Pins lines ~254-260 (and the ``x-mcp-header`` detection at line ~69): the client
    does not mirror headers, so calling such a tool would send the wrong request; it
    is excluded from the described set while every other tool is kept.
    """
    from arcagent.extension.mcp_attachment import SdkMcpClient

    # Partial fake session: only list_tools is implemented, which is all describe uses.
    client = SdkMcpClient(lambda: _TwoToolSession())  # type: ignore[arg-type, return-value]

    specs = await client.describe_tools()

    names = {spec.name for spec in specs}
    assert "clean" in names
    assert "mirrored" not in names


async def test_for_http_forwards_headers_and_initializes_before_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP factory carries auth headers to the transport and initializes first.

    Pins lines ~168-175 — the ``for_http`` factory body, bypassed by the composio
    tests that monkeypatch ``for_http`` wholesale. Stubs the SDK transport and session
    to assert (a) the ``headers`` mapping reaches ``streamablehttp_client`` and
    (b) ``session.initialize()`` is awaited before the session is yielded.
    """
    from arcagent.extension import mcp_attachment

    captured: dict[str, Any] = {}

    class _StubHttp:
        def __call__(self, url: str, headers: dict[str, str] | None = None) -> _StubHttp:
            captured["url"] = url
            captured["headers"] = headers
            return self

        async def __aenter__(self) -> tuple[str, str, str]:
            return ("read-stream", "write-stream", "extra")

        async def __aexit__(self, *exc: object) -> bool:
            return False

    class _StubSession:
        def __init__(self, read: str, write: str, **kwargs: Any) -> None:
            captured["streams"] = (read, write)
            # The client now bounds the session and declares its identity; the stub
            # accepts (and records) those so this factory test tracks the real call.
            captured["session_kwargs"] = kwargs
            self.initialized = False

        async def __aenter__(self) -> _StubSession:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def initialize(self) -> None:
            self.initialized = True

    monkeypatch.setattr(mcp_attachment, "streamablehttp_client", _StubHttp())
    monkeypatch.setattr(mcp_attachment, "ClientSession", _StubSession)

    client = mcp_attachment.SdkMcpClient.for_http(
        url="https://vendor/mcp", headers={"x-api-key": "k"}
    )

    async with client._session_factory() as session:
        # Awaited inside the factory before the yield reached this block. The stub
        # session carries `.initialized`; the real ClientSession type does not.
        assert session.initialized is True  # type: ignore[attr-defined]

    assert captured["url"] == "https://vendor/mcp"
    assert captured["headers"] == {"x-api-key": "k"}
    assert captured["streams"] == ("read-stream", "write-stream")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
