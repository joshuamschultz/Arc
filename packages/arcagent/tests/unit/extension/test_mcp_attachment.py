"""McpAttachment — MCP as one implementation of the hook (SPEC-062 COMP-005, REQ-279).

D-569 makes a vetted CLI the default attachment and MCP the *option*, so the
thing worth proving here is not that MCP works but that it fits: the same
unchanged ``ExtensionAttachment`` Protocol, reached through the same four
methods, with nothing in core knowing a wire protocol exists. Every assertion
below is written against that contract rather than against an MCP-shaped one.

The protocol target is the **2026-07-28 stateless revision**, which removed the
``initialize`` handshake and protocol-level sessions. That deletion is the
security property under test, not a convenience: a client that quietly falls
back to ``initialize`` ends up speaking legacy semantics to a server that never
validated the handshake, which the specification calls out explicitly. So the
tests assert the *absence* of a handshake and of session identifiers, in the
recorded traffic and in the module source, and they assert ``server/discover``
is the probe used to tell a modern server from a legacy one.

Three splits carry the rest of the file:

* **Protocol failure vs tool failure.** A JSON-RPC ``error`` object means the
  request itself was wrong and raises; a success envelope with ``isError: true``
  is the tool reporting a real answer the agent can self-correct from, and must
  reach it as a readable :class:`ToolResult`.
* **``input_required`` is not completion.** A multi-round-trip result carries no
  content the agent may act on; mistaking it for success is a silent wrong
  answer, so it maps to its own outcome.
* **Server-supplied annotations are untrusted.** The specification says so in as
  many words. Classification and capability tags feed the trifecta gate, so they
  come from the manifest; a server that claims ``readOnlyHint`` changes nothing.
"""

from __future__ import annotations

import inspect
import json
import logging
from types import ModuleType
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from arcagent.extension.mcp_attachment import McpAttachment

#: The revision under test. Every request must carry it, in the body and — on
#: HTTP — in the mirrored header.
PROTOCOL_VERSION = "2026-07-28"

_META_VERSION = "io.modelcontextprotocol/protocolVersion"
_META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"

#: Anything in the module source from the era this revision deleted. A client
#: that keeps one of these around has kept the fallback the spec warns about.
_LEGACY_MECHANISMS = (
    '"initialize"',
    "'initialize'",
    "Mcp-Session-Id",
    "mcp-session-id",
    "Last-Event-ID",
)

#: The vendor SDK is a declared dependency of this package but must not be used
#: here (CON-7): the wire is ours to audit.
_SDK_IMPORTS = ("import mcp", "from mcp")


def _module() -> ModuleType:
    import arcagent.extension.mcp_attachment as module

    return module


# --- a server we control ----------------------------------------------------


def _result(**fields: Any) -> dict[str, Any]:
    """A success envelope body, ``resultType`` included as this revision requires."""
    return {"result": {"resultType": "complete", **fields}}


def _error(code: int, message: str, **data: Any) -> dict[str, Any]:
    """A JSON-RPC error body — a protocol failure, never a tool outcome."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"error": error}


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


_WEATHER = {
    "name": "get_weather",
    "title": "Weather Information Provider",
    "description": "Get current weather information for a location",
    "inputSchema": {
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "required": ["location"],
    },
}

_DELETE = {
    "name": "delete_record",
    "description": "Delete a record",
    "inputSchema": {"type": "object", "additionalProperties": False},
}


class _FakeTransport:
    """Stands in for a wire, recording the exact JSON-RPC messages sent over it.

    The attachment's job is to build correct envelopes and read results
    correctly; asserting on what it handed the transport is how that is checked
    without a server in the loop. The two real transports get their own tests.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.scripted: dict[str, list[dict[str, Any]]] = {}
        self.errors: list[BaseException] = []
        self.always: BaseException | None = None
        self.closed = False

    def reply(self, method: str, *bodies: dict[str, Any]) -> None:
        """Queue the bodies this method answers with, the last one repeating."""
        self.scripted.setdefault(method, []).extend(bodies)

    def requirements(self) -> list[Any]:
        return []

    async def send(self, message: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        self.sent.append(message)
        if self.always is not None:
            raise self.always
        if self.errors:
            raise self.errors.pop(0)
        queue = self.scripted.get(message["method"])
        if not queue:
            raise AssertionError(f"no scripted reply for {message['method']}")
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        return {"jsonrpc": "2.0", "id": message["id"], **body}

    async def close(self) -> None:
        self.closed = True

    @property
    def methods(self) -> list[str]:
        return [message["method"] for message in self.sent]

    def last(self, method: str) -> dict[str, Any]:
        for message in reversed(self.sent):
            if message["method"] == method:
                return message
        raise AssertionError(f"{method} was never sent")


@pytest.fixture
def transport() -> _FakeTransport:
    return _FakeTransport()


def _attach(transport: _FakeTransport, **kwargs: Any) -> McpAttachment:
    """An attachment with retry backoff removed so the tests do not sleep."""
    module = _module()
    kwargs.setdefault("resilience", module.McpResilience(backoff_seconds=0.0))
    attachment: McpAttachment = module.McpAttachment(transport, **kwargs)
    return attachment


@pytest.fixture
def mcp(transport: _FakeTransport) -> McpAttachment:
    return _attach(transport)


# --- the hook contract (REQ-278, REQ-279) -----------------------------------


def test_satisfies_the_same_extension_attachment_protocol_as_every_other_shape(
    mcp: McpAttachment,
) -> None:
    """The whole point of D-569's option: MCP is an implementation, not the hook."""
    from arcagent.extension.attachment import ExtensionAttachment

    assert isinstance(mcp, ExtensionAttachment)


def test_the_hook_contract_is_not_edited_to_admit_mcp() -> None:
    """Adding a connection must touch no core file — the Protocol names no wire."""
    from arcagent.extension import attachment

    source = inspect.getsource(attachment)

    assert "mcp" not in source.lower()
    assert "json-rpc" not in source.lower()
    assert "jsonrpc" not in source.lower()


def test_no_vendor_sdk_is_used() -> None:
    """CON-7. The transport is ours so the bytes on the wire are auditable."""
    source = inspect.getsource(_module())

    assert [name for name in _SDK_IMPORTS if name in source] == []


# --- statelessness: what the 2026-07-28 revision deleted --------------------


async def test_no_initialize_handshake_precedes_a_tool_listing(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """The revision is stateless: tools/list is the first thing on the wire."""
    transport.reply("tools/list", _result(tools=[_WEATHER]))

    await mcp.describe_tools()

    assert transport.methods == ["tools/list"]


async def test_no_initialize_handshake_precedes_a_call(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    transport.reply("tools/call", _result(content=[_text("72F")], isError=False))

    await mcp.invoke("get_weather", {"location": "New York"})

    assert transport.methods == ["tools/call"]


def test_the_module_retains_no_legacy_handshake_or_session_mechanism() -> None:
    """A silent fallback to initialize speaks legacy semantics to a modern method."""
    source = inspect.getsource(_module())

    assert [name for name in _LEGACY_MECHANISMS if name in source] == []


async def test_every_request_carries_the_protocol_version_in_meta(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """Required on every request — the server keeps no connection state to infer it."""
    transport.reply("tools/list", _result(tools=[_WEATHER]))
    transport.reply("tools/call", _result(content=[_text("72F")]))

    await mcp.describe_tools()
    await mcp.invoke("get_weather", {"location": "New York"})

    assert transport.sent
    for message in transport.sent:
        meta = message["params"]["_meta"]
        assert meta[_META_VERSION] == PROTOCOL_VERSION


async def test_every_request_carries_client_capabilities_in_meta(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """Also required: a server may not assume a capability the client never declared."""
    transport.reply("tools/list", _result(tools=[_WEATHER]))

    await mcp.describe_tools()

    assert _META_CAPABILITIES in transport.last("tools/list")["params"]["_meta"]


async def test_requests_identify_the_client(mcp: McpAttachment, transport: _FakeTransport) -> None:
    transport.reply("tools/list", _result(tools=[_WEATHER]))

    await mcp.describe_tools()

    info = transport.last("tools/list")["params"]["_meta"][_META_CLIENT_INFO]
    assert info["name"]
    assert info["version"]


async def test_request_ids_are_unique_and_never_null(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """JSON-RPC forbids a null id, and MCP forbids reusing an in-flight one."""
    transport.reply("tools/call", _result(content=[_text("ok")]))

    await mcp.invoke("get_weather", {"location": "a"})
    await mcp.invoke("get_weather", {"location": "b"})

    ids = [message["id"] for message in transport.sent]
    assert all(value is not None for value in ids)
    assert len(set(ids)) == len(ids)


# --- tools/list (REQ-279) ---------------------------------------------------


async def test_tools_list_envelope_is_the_declared_shape(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    transport.reply("tools/list", _result(tools=[_WEATHER]))

    await mcp.describe_tools()

    message = transport.last("tools/list")
    assert message["jsonrpc"] == "2.0"
    assert message["method"] == "tools/list"


async def test_served_tools_become_tool_specs(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    transport.reply("tools/list", _result(tools=[_WEATHER, _DELETE]))

    specs = {spec.name: spec for spec in await mcp.describe_tools()}

    assert sorted(specs) == ["delete_record", "get_weather"]
    assert specs["get_weather"].description == _WEATHER["description"]
    assert specs["get_weather"].input_schema == _WEATHER["inputSchema"]


async def test_classification_and_tags_come_from_the_manifest_not_the_server(
    transport: _FakeTransport,
) -> None:
    """Server annotations are untrusted; these two feed the trifecta gate."""
    module = _module()
    transport.reply(
        "tools/list",
        _result(
            tools=[
                {**_WEATHER, "annotations": {"readOnlyHint": True, "destructiveHint": False}},
                {**_DELETE, "annotations": {"readOnlyHint": True}},
            ]
        ),
    )
    mcp = _attach(
        transport,
        tools={
            "get_weather": module.McpToolPolicy(
                classification="read_only", capability_tags=["network_egress"]
            )
        },
    )

    specs = {spec.name: spec for spec in await mcp.describe_tools()}

    assert specs["get_weather"].classification == "read_only"
    assert specs["get_weather"].capability_tags == ["network_egress"]
    # The manifest says nothing about delete_record, so the server's readOnlyHint
    # must not buy it the cheap treatment (REQ-269).
    assert specs["delete_record"].classification == "state_modifying"
    assert specs["delete_record"].capability_tags == []


async def test_pagination_is_followed_so_a_tool_list_is_never_truncated(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """A silently short tool list looks like a working connection missing verbs."""
    transport.reply(
        "tools/list",
        _result(tools=[_WEATHER], nextCursor="page-2"),
        _result(tools=[_DELETE]),
    )

    specs = await mcp.describe_tools()

    assert sorted(spec.name for spec in specs) == ["delete_record", "get_weather"]
    assert transport.sent[1]["params"]["cursor"] == "page-2"


async def test_a_repeating_cursor_is_refused_rather_than_looped_on(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """A server that always returns the same cursor would spin the agent forever."""
    from arcagent.core.errors import ExtensionError

    transport.reply("tools/list", _result(tools=[_WEATHER], nextCursor="same"))

    with pytest.raises(ExtensionError):
        await mcp.describe_tools()


async def test_a_tool_requiring_header_mirroring_is_excluded_with_a_warning(
    mcp: McpAttachment, transport: _FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    """Calling it would lose a routing parameter the server validates on; omit it."""
    mirrored = {
        "name": "execute_sql",
        "description": "Run SQL",
        "inputSchema": {
            "type": "object",
            "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
        },
    }
    transport.reply("tools/list", _result(tools=[_WEATHER, mirrored]))

    with caplog.at_level(logging.WARNING):
        specs = await mcp.describe_tools()

    assert [spec.name for spec in specs] == ["get_weather"]
    assert any("execute_sql" in message for message in caplog.messages)


async def test_a_malformed_tool_entry_is_a_protocol_failure(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """A nameless tool cannot be registered, policed, or audited."""
    from arcagent.core.errors import ExtensionError

    transport.reply("tools/list", _result(tools=[{"description": "no name"}]))

    with pytest.raises(ExtensionError):
        await mcp.describe_tools()


# --- tools/call (REQ-279, REQ-270, REQ-271) ---------------------------------


async def test_tools_call_envelope_carries_the_name_and_arguments(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    transport.reply("tools/call", _result(content=[_text("72F")]))

    await mcp.invoke("get_weather", {"location": "New York"})

    message = transport.last("tools/call")
    assert message["method"] == "tools/call"
    assert message["params"]["name"] == "get_weather"
    assert message["params"]["arguments"] == {"location": "New York"}


async def test_a_successful_call_returns_the_text_content(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    from arcagent.extension.attachment import ToolOutcome

    transport.reply("tools/call", _result(content=[_text("72F"), _text("cloudy")]))

    result = await mcp.invoke("get_weather", {"location": "New York"})

    assert result.tool == "get_weather"
    assert result.outcome is ToolOutcome.OK
    assert "72F" in result.content
    assert "cloudy" in result.content


async def test_structured_content_is_preserved_verbatim(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """The schema-conforming form is what a caller should act on when offered."""
    transport.reply(
        "tools/call",
        _result(content=[_text("summary")], structuredContent={"temperature": 22.5}),
    )

    result = await mcp.invoke("get_weather", {"location": "New York"})

    assert json.loads(result.content) == {"temperature": 22.5}


async def test_an_empty_result_never_looks_like_a_silent_success(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    from arcagent.extension.attachment import ToolOutcome

    transport.reply("tools/call", _result(content=[]))

    result = await mcp.invoke("get_weather", {"location": "New York"})

    assert result.outcome is ToolOutcome.OK
    assert result.content.strip() != ""


async def test_a_tool_execution_error_reaches_the_agent_and_does_not_raise(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """isError is actionable feedback the model self-corrects from (REQ-270)."""
    from arcagent.extension.attachment import ToolOutcome

    transport.reply(
        "tools/call",
        _result(content=[_text("Invalid departure date: must be in the future.")], isError=True),
    )

    result = await mcp.invoke("get_weather", {"location": "New York"})

    assert result.outcome is ToolOutcome.ERROR
    assert "must be in the future" in result.content


async def test_a_jsonrpc_error_is_a_protocol_failure_and_raises(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """The request itself was wrong; there is no tool outcome to report (REQ-271)."""
    from arcagent.core.errors import ExtensionError

    transport.reply("tools/call", _error(-32602, "Unknown tool: invalid_tool_name"))

    with pytest.raises(ExtensionError) as excinfo:
        await mcp.invoke("nope", {})

    assert "Unknown tool" in str(excinfo.value)


async def test_the_two_error_tiers_are_never_conflated(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """A protocol error must not arrive as a readable result the model retries."""
    from arcagent.core.errors import ExtensionError
    from arcagent.extension.attachment import ToolResult

    transport.reply("tools/call", _error(-32603, "Internal error"))

    with pytest.raises(ExtensionError) as excinfo:
        await mcp.invoke("get_weather", {"location": "New York"})

    assert not isinstance(excinfo.value, ToolResult)


async def test_input_required_is_its_own_outcome_and_never_completion(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """Treating a half-finished round trip as success is a silent wrong answer."""
    from arcagent.extension.attachment import ToolOutcome

    transport.reply(
        "tools/call",
        {
            "result": {
                "resultType": "input_required",
                "inputRequests": {
                    "github_login": {
                        "method": "elicitation/create",
                        "params": {"mode": "form", "message": "Provide your username"},
                    }
                },
                "requestState": "eyJsb2NhdGlvbiI6Ik5ldyBZb3JrIn0",
            }
        },
    )

    result = await mcp.invoke("get_weather", {"location": "New York"})

    assert result.outcome is ToolOutcome.INPUT_REQUIRED
    assert "github_login" in result.content


async def test_an_absent_result_type_is_read_as_complete(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """Earlier revisions omitted it; the spec requires reading that as complete."""
    from arcagent.extension.attachment import ToolOutcome

    transport.reply("tools/call", {"result": {"content": [_text("72F")]}})

    result = await mcp.invoke("get_weather", {"location": "New York"})

    assert result.outcome is ToolOutcome.OK


async def test_an_unrecognized_result_type_is_invalid_and_raises(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """The spec is explicit: a resultType the client does not know is invalid."""
    from arcagent.core.errors import ExtensionError

    transport.reply("tools/call", {"result": {"resultType": "partially_done"}})

    with pytest.raises(ExtensionError):
        await mcp.invoke("get_weather", {"location": "New York"})


# --- probe: telling a modern server from a legacy one -----------------------


async def test_probe_uses_server_discover_and_reports_the_live_tools(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    transport.reply("server/discover", _result(supportedVersions=[PROTOCOL_VERSION]))
    transport.reply("tools/list", _result(tools=[_WEATHER]))

    result = await mcp.probe()

    assert transport.methods[0] == "server/discover"
    assert result.reachable is True
    assert [spec.name for spec in result.tools] == ["get_weather"]


async def test_probe_reports_a_legacy_server_as_unreachable_without_falling_back(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """A legacy server answers the probe with an implementation-defined error.

    Falling back to ``initialize`` is what the spec warns against: the server
    would then process ``tools/call`` under legacy semantics. Refusing is the
    deterministic failure.
    """
    transport.reply("server/discover", _error(-32601, "Method not found"))

    result = await mcp.probe()

    assert result.reachable is False
    assert "tools/list" not in transport.methods
    assert result.tools == []


async def test_probe_names_the_versions_a_modern_server_does_support(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """-32022 means modern-but-mismatched; the operator needs to see the list."""
    transport.reply(
        "server/discover",
        _error(-32022, "Unsupported protocol version", supported=["2025-11-25"]),
    )

    result = await mcp.probe()

    assert result.reachable is False
    assert "2025-11-25" in result.detail


async def test_probe_answers_rather_than_raising_when_the_server_is_unreachable(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    """Probing IS the reachability test — it reports, it does not blow up."""
    from arcagent.core.errors import ExtensionError

    transport.always = ExtensionError(code="EXTENSION_TRANSPORT_FAILED", message="refused")

    result = await mcp.probe()

    assert result.reachable is False
    assert "refused" in result.detail


# --- resilience (REQ-270, REQ-271) ------------------------------------------


async def test_a_timeout_becomes_a_readable_result_after_the_retries_are_spent(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    from arcagent.extension.attachment import ToolOutcome

    transport.always = TimeoutError()

    result = await mcp.invoke("get_weather", {"location": "New York"})

    assert result.outcome is ToolOutcome.ERROR
    assert len(transport.sent) > 1


async def test_a_transient_timeout_is_retried_and_then_succeeds(
    mcp: McpAttachment, transport: _FakeTransport
) -> None:
    from arcagent.extension.attachment import ToolOutcome

    transport.errors = [TimeoutError()]
    transport.reply("tools/call", _result(content=[_text("72F")]))

    result = await mcp.invoke("get_weather", {"location": "New York"})

    assert result.outcome is ToolOutcome.OK
    assert len(transport.sent) == 2


async def test_a_tripped_breaker_returns_a_structured_error_without_calling_out(
    transport: _FakeTransport,
) -> None:
    """Tools stay registered — a connection going dark is its own outage."""
    from arcagent.extension.attachment import ToolOutcome

    module = _module()
    mcp = _attach(
        transport,
        resilience=module.McpResilience(
            backoff_seconds=0.0, max_attempts=1, failure_threshold=2, reset_after_seconds=60.0
        ),
    )
    transport.always = TimeoutError()

    for _ in range(2):
        await mcp.invoke("get_weather", {"location": "New York"})
    before = len(transport.sent)
    result = await mcp.invoke("get_weather", {"location": "New York"})

    assert result.outcome is ToolOutcome.ERROR
    assert len(transport.sent) == before


async def test_a_protocol_failure_does_not_trip_the_breaker(
    transport: _FakeTransport,
) -> None:
    """The connection is working perfectly; it is the request that was wrong."""
    from arcagent.core.errors import ExtensionError

    module = _module()
    mcp = _attach(
        transport,
        resilience=module.McpResilience(backoff_seconds=0.0, failure_threshold=2),
    )
    transport.reply("tools/call", _error(-32602, "Unknown tool"))

    for _ in range(3):
        with pytest.raises(ExtensionError):
            await mcp.invoke("nope", {})

    assert len(transport.sent) == 3


# --- requirements (REQ-262) -------------------------------------------------


def test_requirements_are_the_transport_s_to_declare(transport: _FakeTransport) -> None:
    """A spawned server needs a binary; a hosted one needs a credential.

    The attachment declares neither itself — it forwards what its transport
    knows, which is why swapping stdio for HTTP changes the requirement list
    without changing this class.
    """
    assert _attach(transport).requirements() == []


# --- the HTTP transport -----------------------------------------------------


def _http(handler: Any, **kwargs: Any) -> Any:
    module = _module()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return module.HttpTransport(url="https://example.test/mcp", client=client, **kwargs)


def _json_response(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
    sent = json.loads(request.content)
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": sent["id"], **body})


_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


async def test_http_posts_to_the_single_mcp_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json_response(request, _result(tools=[]))

    transport = _http(handler)
    await transport.send(_LIST, timeout=5)

    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://example.test/mcp"


async def test_http_accepts_both_a_json_object_and_an_event_stream() -> None:
    """The client must support either answer, so it must advertise both."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json_response(request, _result(tools=[]))

    transport = _http(handler)
    await transport.send(_LIST, timeout=5)

    accept = seen[0].headers["accept"]
    assert "application/json" in accept
    assert "text/event-stream" in accept


async def test_http_mirrors_the_protocol_version_and_method_into_headers() -> None:
    """Required for compliance, and a mismatch is a 400 the server must reject."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json_response(request, _result(content=[_text("72F")]))

    transport = _http(handler)
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "get_weather",
            "arguments": {},
            "_meta": {_META_VERSION: PROTOCOL_VERSION},
        },
    }
    await transport.send(message, timeout=5)

    request = seen[0]
    assert request.headers["mcp-protocol-version"] == PROTOCOL_VERSION
    assert request.headers["mcp-method"] == "tools/call"
    assert request.headers["mcp-name"] == "get_weather"


async def test_http_reads_the_final_response_out_of_an_event_stream() -> None:
    """Notifications may precede it; only the matching response is the answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        final = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": sent["id"],
                "result": {"resultType": "complete", "content": [_text("72F")]},
            }
        )
        body = (
            'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
            ":keep-alive\n\n"
            f"data: {final}\n\n"
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode()
        )

    transport = _http(handler)
    reply = await transport.send(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "get_weather"}},
        timeout=5,
    )

    assert reply["id"] == 7
    assert reply["result"]["content"][0]["text"] == "72F"


async def test_http_returns_a_modern_error_body_rather_than_masking_it_as_transport() -> None:
    """A modern server uses 400 for its own JSON-RPC errors; that is not a hang-up."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        return httpx.Response(
            400, json={"jsonrpc": "2.0", "id": sent["id"], **_error(-32022, "Unsupported")}
        )

    transport = _http(handler)
    reply = await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, timeout=5
    )

    assert reply["error"]["code"] == -32022


async def test_http_treats_a_non_jsonrpc_error_body_as_a_transport_failure() -> None:
    from arcagent.core.errors import ExtensionError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    transport = _http(handler)

    with pytest.raises(ExtensionError):
        await transport.send(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, timeout=5
        )


async def test_http_refuses_an_unbounded_body_rather_than_buffering_it() -> None:
    """A hostile server must not be able to exhaust the agent's memory (LLM10)."""
    from arcagent.core.errors import ExtensionError

    module = _module()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (module.MAX_MESSAGE_BYTES + 1))

    transport = _http(handler)

    with pytest.raises(ExtensionError):
        await transport.send(_LIST, timeout=5)


async def test_http_sends_the_credential_and_never_renders_it() -> None:
    """A Secret is revealed at the wire and nowhere else (REQ-265, LLM07)."""
    from arcagent.extension.secrets import Secret

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json_response(request, _result(tools=[]))

    transport = _http(handler, token=Secret("s3cr3t-token"))
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, timeout=5
    )

    assert seen[0].headers["authorization"] == "Bearer s3cr3t-token"
    assert "s3cr3t-token" not in repr(transport)


def test_http_requirements_name_the_credential_the_operator_must_supply() -> None:
    from arcagent.extension.attachment import RequirementKind

    transport = _http(lambda request: httpx.Response(200, json={}), credential_field="api_token")

    requirements = transport.requirements()

    assert [r.name for r in requirements] == ["api_token"]
    assert requirements[0].kind is RequirementKind.CREDENTIAL


# --- the stdio transport ----------------------------------------------------


class _FakeStdin:
    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None


class _FakeProcess:
    """A subprocess whose streams are real asyncio readers fed with fixed bytes."""

    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        import asyncio

        self.returncode: int | None = None
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()


class _FakeLauncher:
    """Stands in for ProcessLauncher, recording that acquisition went through it."""

    def __init__(self, process: _FakeProcess) -> None:
        self.process = process
        self.acquired: list[Any] = []

    async def acquire(self, definition: Any) -> Any:
        from arcagent.extension.launcher import ProcessHandle

        self.acquired.append(definition)
        return ProcessHandle(
            key=definition.key,
            process=self.process,  # type: ignore[arg-type]  # duck-typed streams are the point
            idle_timeout_seconds=definition.idle_timeout_seconds,
            last_used=0.0,
        )


def _definition() -> Any:
    from arcagent.extension.launcher import ProcessDefinition

    return ProcessDefinition(key="weather", argv=["weather-mcp", "--stdio"])


def _stdio(process: _FakeProcess) -> tuple[Any, _FakeLauncher]:
    module = _module()
    launcher = _FakeLauncher(process)
    transport = module.StdioTransport(launcher=launcher, definition=_definition())
    return transport, launcher


def _line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload) + "\n").encode()


async def test_stdio_spawns_only_through_the_process_launcher() -> None:
    """Env scrubbing, pinning, and sandbox policy all ride that one path (REQ-292)."""
    process = _FakeProcess(_line({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}))
    transport, launcher = _stdio(process)

    await transport.send(_LIST, timeout=5)

    assert [definition.key for definition in launcher.acquired] == ["weather"]


def test_stdio_has_no_second_spawn_path() -> None:
    """A direct exec here would bypass the launcher's scrub, pin, and policy."""
    source = inspect.getsource(_module())

    assert "create_subprocess_exec" not in source
    assert "create_subprocess_shell" not in source
    assert "subprocess.Popen" not in source


async def test_stdio_writes_one_newline_delimited_message_with_no_embedded_newline() -> None:
    """The framing IS the protocol here — an embedded newline splits the message."""
    process = _FakeProcess(_line({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}))
    transport, _ = _stdio(process)

    await transport.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"arguments": {"note": "first\nsecond"}},
        },
        timeout=5,
    )

    written = process.stdin.written
    assert written.endswith(b"\n")
    assert written.count(b"\n") == 1
    assert json.loads(written)["params"]["arguments"]["note"] == "first\nsecond"


async def test_stdio_skips_notifications_and_unrelated_ids() -> None:
    """One shared channel carries everything; only the matching id is the answer."""
    stdout = (
        _line({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
        + _line({"jsonrpc": "2.0", "id": 99, "result": {"resultType": "complete"}})
        + _line({"jsonrpc": "2.0", "id": 4, "result": {"resultType": "complete", "content": []}})
    )
    transport, _ = _stdio(_FakeProcess(stdout))

    reply = await transport.send(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {}}, timeout=5
    )

    assert reply["id"] == 4


async def test_stdio_reads_a_message_larger_than_the_default_stream_line_limit() -> None:
    """asyncio caps a line at 64 KiB; a real tool list blows straight through it."""
    payload = "x" * (200 * 1024)
    stdout = _line(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"resultType": "complete", "content": [{"type": "text", "text": payload}]},
        }
    )
    transport, _ = _stdio(_FakeProcess(stdout))

    reply = await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}, timeout=5
    )

    assert reply["result"]["content"][0]["text"] == payload


async def test_stdio_output_on_stderr_is_not_a_failure_signal() -> None:
    """The spec says so outright: stderr is free-form logging, not an error."""
    process = _FakeProcess(
        _line({"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete", "content": []}}),
        stderr=b"loaded 4 plugins\n",
    )
    transport, _ = _stdio(process)

    reply = await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, timeout=5
    )

    assert "error" not in reply


async def test_stdio_reports_a_dead_server_as_a_transport_failure() -> None:
    """Nothing answered, so there is no result to interpret — this raises."""
    from arcagent.core.errors import ExtensionError

    transport, _ = _stdio(_FakeProcess(b""))

    with pytest.raises(ExtensionError):
        await transport.send(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, timeout=5
        )


async def test_stdio_refuses_an_unbounded_message_rather_than_buffering_it() -> None:
    """A hostile server must not be able to exhaust the agent's memory (LLM10)."""
    from arcagent.core.errors import ExtensionError

    module = _module()
    transport, _ = _stdio(_FakeProcess(b"x" * (module.MAX_MESSAGE_BYTES + 1)))

    with pytest.raises(ExtensionError):
        await transport.send(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, timeout=5
        )


def test_stdio_requirements_name_the_binary_as_a_host_prerequisite() -> None:
    from arcagent.extension.attachment import RequirementKind

    transport, _ = _stdio(_FakeProcess(b""))

    requirements = transport.requirements()

    assert [r.name for r in requirements] == ["weather-mcp"]
    assert requirements[0].kind is RequirementKind.HOST
