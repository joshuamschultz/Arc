"""SPEC-082 T-1087 (RED) — the streamable-HTTP door responder.

REQ-418 / COMP-001. The door's HTTP surface is a streamable-HTTP responder: one POST
per message, answering JSON (or ``text/event-stream``), capped at 8 MiB, serving
``server/discover`` / ``tools/list`` / ``tools/call``. mTLS is required at federal —
a request that arrives without a client certificate is refused.

This test drives ``arcagent.modules.mcp_server.http_transport.HttpDoor``, an ASGI app
exercised through ``httpx.ASGITransport`` (no live socket, no vendor SDK — CON-7).
The module does not exist yet. The RED is the import: ``No module named
'arcagent.modules.mcp_server.http_transport'``. It goes GREEN when T-1088 adds it.

``ASGITransport`` builds a scope with no TLS extension, so a federal door — which
must find a client certificate — sees none and refuses. That absence is exactly the
condition REQ-418 tests; the personal door serves the same request to prove the
refusal is the federal mTLS rule, not a broken responder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from arcagent.modules.mcp_server.http_transport import HttpDoor
from arcagent.modules.mcp_server.server import McpServer

_EIGHT_MIB = 8 * 1024 * 1024


@dataclass(frozen=True)
class _FakeTool:
    """A duck-typed stand-in for ``RegisteredTool`` — the three fields the door reads."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass
class _FakeRegistry:
    """Minimal registry exposing the ``tools`` mapping ``McpServer`` reads."""

    tools: dict[str, _FakeTool]


def _server() -> McpServer:
    registry = _FakeRegistry(
        tools={"read_file": _FakeTool("read_file", "Read a file"), "list_dir": _FakeTool("list_dir", "List a dir")}
    )
    return McpServer(registry)  # type: ignore[arg-type]


def _tools_list_message() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


@pytest.mark.asyncio
async def test_post_tools_list_returns_the_catalog_over_http() -> None:
    """A POST of a ``tools/list`` request returns the tool catalog as JSON-RPC."""
    door = HttpDoor(_server(), tier="personal")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=door), base_url="http://door"
    ) as client:
        response = await client.post("/", json=_tools_list_message())

    assert response.status_code == 200
    result = response.json()["result"]
    names = {tool["name"] for tool in result["tools"]}
    assert names == {"read_file", "list_dir"}


@pytest.mark.asyncio
async def test_federal_refuses_a_request_without_a_client_certificate() -> None:
    """At federal tier, mTLS is required — a certless request is refused (REQ-418)."""
    door = HttpDoor(_server(), tier="federal")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=door), base_url="http://door"
    ) as client:
        response = await client.post("/", json=_tools_list_message())

    assert response.status_code >= 400
    assert "result" not in response.json()


@pytest.mark.asyncio
async def test_oversized_body_is_rejected() -> None:
    """A body over the 8 MiB cap is refused with 413 before it is parsed."""
    door = HttpDoor(_server(), tier="personal")
    oversized = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"blob": "x" * (_EIGHT_MIB + 1)}}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=door), base_url="http://door"
    ) as client:
        response = await client.post("/", json=oversized)

    assert response.status_code == 413


# --- /review backfill: raw-ASGI-scope guard branches --------------------------
#
# ``httpx.ASGITransport`` builds a conforming HTTP scope with no TLS extension, so
# it cannot reach the non-POST guard, the content-length short-circuit, a
# chunked-body overflow, the lifespan protocol, or a cert-present positive control.
# These tests drive ``HttpDoor.__call__`` with hand-built scopes so each guard
# branch actually executes.

_TOOLS_LIST_BODY = b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'


async def _drive(
    door: HttpDoor, scope: dict[str, Any], events: list[dict[str, Any]] | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Run one ASGI request against ``door``; return (sent messages, receive calls)."""
    queue = list(events or [{"type": "http.request", "body": b"", "more_body": False}])
    receive_calls = 0

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        if queue:
            return queue.pop(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await door(scope, receive, send)
    return sent, receive_calls


def _status(sent: list[dict[str, Any]]) -> int:
    return int(next(m["status"] for m in sent if m["type"] == "http.response.start"))


def _body(sent: list[dict[str, Any]]) -> dict[str, Any]:
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return json.loads(raw)  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_non_post_method_is_rejected_405() -> None:
    """Only POST carries a JSON-RPC message; a GET is refused with 405."""
    door = HttpDoor(_server(), tier="personal")

    sent, _ = await _drive(door, {"type": "http", "method": "GET", "headers": []})

    assert _status(sent) == 405


@pytest.mark.asyncio
async def test_declared_content_length_over_cap_is_413_before_body_read() -> None:
    """A ``content-length`` over the cap is refused with 413 and the body is never read."""
    door = HttpDoor(_server(), tier="personal", max_body_bytes=1024)
    over = str(2048).encode()

    sent, receive_calls = await _drive(
        door, {"type": "http", "method": "POST", "headers": [(b"content-length", over)]}
    )

    assert _status(sent) == 413
    assert receive_calls == 0, "body was read despite an over-cap content-length"


@pytest.mark.asyncio
async def test_streamed_body_over_cap_is_413() -> None:
    """A body with no declared length that overflows the cap mid-stream is refused 413."""
    door = HttpDoor(_server(), tier="personal", max_body_bytes=4096)
    chunk = b"x" * 2048
    events = [{"type": "http.request", "body": chunk, "more_body": True} for _ in range(4)]
    events.append({"type": "http.request", "body": b"", "more_body": False})

    sent, _ = await _drive(door, {"type": "http", "method": "POST", "headers": []}, events)

    assert _status(sent) == 413


@pytest.mark.asyncio
async def test_malformed_json_body_is_400() -> None:
    """A body that is not valid JSON is refused with 400 (INVALID_REQUEST)."""
    door = HttpDoor(_server(), tier="personal")
    events = [{"type": "http.request", "body": b"{ not json", "more_body": False}]

    sent, _ = await _drive(door, {"type": "http", "method": "POST", "headers": []}, events)

    assert _status(sent) == 400
    assert _body(sent)["error"]["message"] == "malformed JSON"


@pytest.mark.asyncio
async def test_lifespan_scope_answers_startup_and_shutdown() -> None:
    """A hosting ASGI server's lifespan protocol is answered start-to-finish."""
    door = HttpDoor(_server(), tier="personal")
    events = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    sent, _ = await _drive(door, {"type": "lifespan"}, events)

    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]


@pytest.mark.asyncio
async def test_enterprise_without_client_certificate_is_403() -> None:
    """The mTLS gate now covers ENTERPRISE too: a certless request is refused 403.

    This is the just-landed fix — the gate was federal-only before; enterprise is a
    hardened tier and must require the client certificate as well (REQ-418).
    """
    door = HttpDoor(_server(), tier="enterprise")
    events = [{"type": "http.request", "body": _TOOLS_LIST_BODY, "more_body": False}]

    sent, _ = await _drive(door, {"type": "http", "method": "POST", "headers": []}, events)

    assert _status(sent) == 403
    assert _body(sent)["error"]["message"] == "mTLS client certificate required"


@pytest.mark.asyncio
async def test_federal_with_client_certificate_is_served() -> None:
    """Positive control: a federal door WITH a client cert in the TLS extension serves.

    Proves the enterprise/federal 403 is the missing-certificate rule, not a broken
    responder — the same request with a cert chain present is answered 200.
    """
    door = HttpDoor(_server(), tier="federal")
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [],
        "extensions": {"tls": {"client_cert_chain": ["-----BEGIN CERTIFICATE-----"]}},
    }
    events = [{"type": "http.request", "body": _TOOLS_LIST_BODY, "more_body": False}]

    sent, _ = await _drive(door, scope, events)

    assert _status(sent) == 200
    names = {tool["name"] for tool in _body(sent)["result"]["tools"]}
    assert names == {"read_file", "list_dir"}
