"""RED (SPEC-084 regression) — the SDK-backed MCP client must apply its resilience.

SPEC-084 re-based the MCP client on the official ``mcp`` SDK but dropped the bounds
the hand-rolled client enforced: a per-call timeout, a circuit breaker, and bounded
retry raising :class:`~arcagent.extension.mcp_policy.UnavailableError`. The SDK's
``ClientSession`` runs ``anyio.fail_after(None)`` when no ``read_timeout_seconds`` is
set, so a stdio or HTTP server that stops answering hangs an agent turn forever
(LLM10, unbounded consumption). :class:`SdkMcpClient` accepted and stored
``resilience``/``client_name`` but never applied them.

These tests pin the restored behavior against the injected ``session_factory`` seam —
the same seam production fills with a real SDK session — so no socket is needed and
every bound is forced deterministically rather than raced against wall-clock time:

* a hanging call is cut off by the timeout instead of hanging;
* the breaker opens after ``failure_threshold`` failures and short-circuits without
  reopening the wire;
* a transient failure is retried and then succeeds within ``max_attempts``;
* the declared ``client_name`` reaches the server as the SDK ``client_info`` and the
  timeout is threaded into the session as ``read_timeout_seconds``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from arcagent.extension.attachment import ToolOutcome, ToolResult
from arcagent.extension.mcp_policy import McpResilience, UnavailableError


class _HangingSession:
    """A session whose ``call_tool`` never returns, standing in for a dead server."""

    async def __aenter__(self) -> _HangingSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def call_tool(self, name: str, args: dict[str, Any]) -> CallToolResult:
        await asyncio.Event().wait()  # never set — the operation hangs
        raise AssertionError("unreachable")  # pragma: no cover


class _AlwaysFailSession:
    """A session whose ``call_tool`` always raises a transport-style fault."""

    async def __aenter__(self) -> _AlwaysFailSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def call_tool(self, name: str, args: dict[str, Any]) -> CallToolResult:
        raise RuntimeError("connection reset")


class _FlakyThenOkSession:
    """Fails on the first entry, then answers cleanly — shared count across sessions."""

    def __init__(self, state: dict[str, int]) -> None:
        self._state = state

    async def __aenter__(self) -> _FlakyThenOkSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def call_tool(self, name: str, args: dict[str, Any]) -> CallToolResult:
        self._state["calls"] += 1
        if self._state["calls"] == 1:
            raise RuntimeError("first attempt drops")
        return CallToolResult(content=[TextContent(type="text", text="ok")], isError=False)


class _CountingFactory:
    """A zero-arg session factory that records how often it is entered."""

    def __init__(self, session_maker: Any) -> None:
        self._session_maker = session_maker
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return self._session_maker()


async def test_timeout_cuts_off_a_hanging_call() -> None:
    """A call that never returns is bounded by ``timeout_seconds``, not left to hang."""
    from arcagent.extension.mcp_attachment import SdkMcpClient

    factory = _CountingFactory(_HangingSession)
    client = SdkMcpClient(
        factory,
        resilience=McpResilience(timeout_seconds=0.05, max_attempts=1, backoff_seconds=0.0),
    )

    # The outer bound is the test's own safety net: if the client itself hangs, this
    # raises asyncio.TimeoutError (not UnavailableError) and the test fails loudly.
    with pytest.raises(UnavailableError):
        await asyncio.wait_for(client.invoke("slow", {}), timeout=5.0)


async def test_breaker_opens_and_short_circuits_without_reopening_the_wire() -> None:
    """After ``failure_threshold`` failures the next call fails fast, wire untouched."""
    from arcagent.extension.mcp_attachment import SdkMcpClient

    factory = _CountingFactory(_AlwaysFailSession)
    client = SdkMcpClient(
        factory,
        resilience=McpResilience(
            max_attempts=1,
            backoff_seconds=0.0,
            failure_threshold=3,
            reset_after_seconds=30.0,
        ),
    )

    for _ in range(3):
        with pytest.raises(UnavailableError):
            await client.invoke("boom", {})
    assert factory.calls == 3

    # Breaker is open: the next call must be refused before touching the factory.
    with pytest.raises(UnavailableError):
        await client.invoke("boom", {})
    assert factory.calls == 3


async def test_retry_then_success_within_max_attempts() -> None:
    """A single transient failure is retried and the retry's success is returned."""
    from arcagent.extension.mcp_attachment import SdkMcpClient

    state = {"calls": 0}
    factory = _CountingFactory(lambda: _FlakyThenOkSession(state))
    client = SdkMcpClient(
        factory,
        resilience=McpResilience(max_attempts=3, backoff_seconds=0.0),
    )

    result = await client.invoke("echo", {"text": "hi"})

    assert isinstance(result, ToolResult)
    assert result.outcome is ToolOutcome.OK
    assert "ok" in result.content
    assert factory.calls == 2  # failed once, succeeded on the retry


async def test_for_http_carries_client_info_and_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``for_http`` declares ``client_name`` as SDK ``client_info`` and bounds the session."""
    from arcagent.extension import mcp_attachment

    captured: dict[str, Any] = {}

    class _StubHttp:
        def __call__(self, url: str, headers: dict[str, str] | None = None) -> _StubHttp:
            return self

        async def __aenter__(self) -> tuple[str, str, str]:
            return ("read", "write", "extra")

        async def __aexit__(self, *exc: object) -> bool:
            return False

    class _StubSession:
        def __init__(
            self,
            read: str,
            write: str,
            *,
            read_timeout_seconds: timedelta | None = None,
            client_info: Any = None,
        ) -> None:
            captured["read_timeout_seconds"] = read_timeout_seconds
            captured["client_info"] = client_info

        async def __aenter__(self) -> _StubSession:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def initialize(self) -> None:
            return None

    monkeypatch.setattr(mcp_attachment, "streamablehttp_client", _StubHttp())
    monkeypatch.setattr(mcp_attachment, "ClientSession", _StubSession)

    client = mcp_attachment.SdkMcpClient.for_http(
        url="https://vendor/mcp",
        client_name="arc-olivia",
        resilience=McpResilience(timeout_seconds=12.0),
    )
    async with client._session_factory():
        pass

    assert captured["client_info"].name == "arc-olivia"
    assert captured["read_timeout_seconds"] == timedelta(seconds=12.0)


async def test_for_stdio_carries_client_info_and_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``for_stdio`` bounds the local server session and declares its identity too."""
    from arcagent.extension import mcp_attachment

    captured: dict[str, Any] = {}

    class _StubStdio:
        def __call__(self, params: Any) -> _StubStdio:
            return self

        async def __aenter__(self) -> tuple[str, str]:
            return ("read", "write")

        async def __aexit__(self, *exc: object) -> bool:
            return False

    class _StubSession:
        def __init__(
            self,
            read: str,
            write: str,
            *,
            read_timeout_seconds: timedelta | None = None,
            client_info: Any = None,
        ) -> None:
            captured["read_timeout_seconds"] = read_timeout_seconds
            captured["client_info"] = client_info

        async def __aenter__(self) -> _StubSession:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def initialize(self) -> None:
            return None

    monkeypatch.setattr(mcp_attachment, "stdio_client", _StubStdio())
    monkeypatch.setattr(mcp_attachment, "ClientSession", _StubSession)

    client = mcp_attachment.SdkMcpClient.for_stdio(
        command="mcp-server",
        args=["--stdio"],
        client_name="arc-local",
        resilience=McpResilience(timeout_seconds=7.5),
    )
    async with client._session_factory():
        pass

    assert captured["client_info"].name == "arc-local"
    assert captured["read_timeout_seconds"] == timedelta(seconds=7.5)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
