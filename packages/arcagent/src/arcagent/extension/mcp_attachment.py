"""SdkMcpClient — MCP as one implementation of the hook (SPEC-084, REQ-269).

D-569 makes a vetted CLI the default attachment and this the *option*, so the value
of this module is as much proof as capability: a wire protocol with its own
handshake, its own error taxonomy, and two transports reaches the agent through the
same four unchanged methods of
:class:`~arcagent.extension.attachment.ExtensionAttachment` that a local binary
does. Nothing in ``arcagent`` outside this file knows the protocol exists.

SPEC-084 re-bases the client on the official ``mcp`` SDK. The SDK negotiates the
real protocol the ecosystem runs — the Legacy ``initialize`` handshake — instead of
the hand-rolled ``server/discover`` dialect the old client spoke to nobody. The wire
is the SDK's; this module's job is the *boundary*: it drives one already-initialized
:class:`mcp.ClientSession` per operation and adapts every SDK type to a plain hook
value before it can escape (:func:`_sdk_content`, :meth:`SdkMcpClient._spec`).

Two security positions are deliberate. Server-supplied ``annotations`` are ignored
entirely — the specification calls them untrusted, and ``classification`` and
``capability_tags`` are what feed the trifecta gate, so they come from the manifest
via :class:`McpToolPolicy` and default to the restrictive value (REQ-269). And a
spawned stdio server's environment is scrubbed and its argv is wrapped by the
deployment's sandbox policy *before* it reaches :meth:`SdkMcpClient.for_stdio` — the
connector builder does that (``modules/connectors/attachments.py``), passing the
scrubbed environment straight into ``StdioServerParameters.env``.

CON-7 is LLM-scoped, and the ``mcp`` SDK is the sanctioned, version-pinned dependency
this seam is built on (REQ-456), so this file is the one place allowed to import it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from typing import Any, TypeVar

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, Implementation, Tool

from arcagent import __version__
from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.mcp_policy import (
    DEFAULT_POLICY as _DEFAULT_POLICY,
)
from arcagent.extension.mcp_policy import (
    CircuitBreaker as _CircuitBreaker,
)
from arcagent.extension.mcp_policy import (
    McpResilience,
    McpToolPolicy,
)
from arcagent.extension.mcp_policy import (
    UnavailableError as _UnavailableError,
)

_logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: Marks a tool parameter the server wants mirrored into an HTTP header. This
#: client does not mirror, so such a tool is excluded rather than called wrongly.
_HEADER_MIRROR_KEY = "x-mcp-header"


def _requires_header_mirroring(node: object) -> bool:
    """True when a schema asks for a parameter to be mirrored into a header."""
    if isinstance(node, dict):
        if _HEADER_MIRROR_KEY in node:
            return True
        return any(_requires_header_mirroring(value) for value in node.values())
    if isinstance(node, list):
        return any(_requires_header_mirroring(item) for item in node)
    return False


def _sdk_content(result: CallToolResult) -> str:
    """The text an agent reads out of one SDK tool result, adapted at the boundary.

    Nothing SDK-shaped leaves this function: it returns the same plain ``str`` the
    hand-rolled path returns, so :class:`SdkMcpClient` never leaks an ``mcp`` type
    into a :class:`~arcagent.extension.attachment.ToolResult`. Structured content
    wins when the server offers it — it is the schema-conforming form, and the text
    block beside it is a serialisation of the same thing.
    """
    if result.structuredContent is not None:
        return json.dumps(result.structuredContent)
    parts: list[str] = []
    for block in result.content:
        if getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
        else:
            # A non-text block still happened; dropping it silently would shorten
            # the answer without saying so.
            parts.append(f"[{getattr(block, 'type', 'unknown')}]")
    return "\n".join(part for part in parts if part)


class SdkMcpClient:
    """Attaches an MCP server through the official ``mcp`` SDK (SPEC-084, REQ-269).

    Satisfies :class:`~arcagent.extension.attachment.ExtensionAttachment` — the four
    unchanged methods every attachment shape reaches the agent through — but
    negotiates the real protocol handshake the ecosystem runs by delegating the wire
    to a supplied SDK session rather than hand-rolling JSON-RPC. Every SDK type is
    adapted to a hook value type at this boundary, so nothing ``mcp``-shaped escapes.

    The session factory is the injection seam. Production builds it around the SDK's
    real transports through :meth:`for_http` and :meth:`for_stdio`; a test builds it
    around the in-memory fixture. Either way this client only *drives* an
    already-initialized session — it never chooses or re-initializes the wire.

    Args:
        session_factory: A zero-argument factory that, each time it is entered,
            yields a *fresh*, already-initialized :class:`mcp.ClientSession`. This
            client opens one session per operation and closes it via ``async with``.
            Production wraps ``streamablehttp_client`` / ``stdio_client`` (T-1144);
            a test wraps the in-memory fixture. The client never re-initializes what
            the factory hands it.
        tools: Per-tool manifest policy. Classification and capability tags come
            only from here and default to the restrictive value; server-supplied
            ``annotations`` are untrusted and ignored (REQ-269).
        resilience: Timeout, retry, and circuit-breaker bounds, applied to every
            operation. Each call is bounded by ``timeout_seconds`` (so a stdio or
            HTTP server that stops answering cannot hang a turn — LLM10), retried up
            to ``max_attempts`` with exponential backoff, and guarded by a breaker
            that opens after ``failure_threshold`` transport failures; repeated
            failure or an open breaker raises
            :class:`~arcagent.extension.mcp_policy.UnavailableError`. The production
            factories additionally thread ``timeout_seconds`` into the SDK session as
            ``read_timeout_seconds``, bounding each SDK request in depth.
        client_name: Identity reported to the server as the SDK ``client_info`` by the
            production factories, for its logs and ours.
        requirements: What the host or operator must supply. The session-factory
            seam has no transport object to ask, so requirements are injected.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[ClientSession]],
        *,
        tools: Mapping[str, McpToolPolicy] | None = None,
        resilience: McpResilience | None = None,
        client_name: str = "arc",
        requirements: list[Requirement] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._tools = dict(tools or {})
        self._resilience = resilience or McpResilience()
        self._client_name = client_name
        self._requirements = list(requirements or [])
        self._breaker = _CircuitBreaker(
            self._resilience.failure_threshold, self._resilience.reset_after_seconds
        )

    # --- production session factories (the one place the SDK transports live) ---

    @classmethod
    def for_http(
        cls,
        *,
        url: str,
        headers: Mapping[str, str] | None = None,
        tools: Mapping[str, McpToolPolicy] | None = None,
        resilience: McpResilience | None = None,
        client_name: str = "arc",
        requirements: list[Requirement] | None = None,
    ) -> SdkMcpClient:
        """A client for a hosted MCP server reached over Streamable HTTP.

        The factory opens a fresh SDK HTTP transport and an initialized session each
        time it is entered, and closes both on exit — one connection per operation,
        never a pooled one held open. ``headers`` carries auth (the connector builder
        passes ``Authorization: Bearer <token>``); it is captured here, not per call,
        because the SDK transport takes a plain header mapping.
        """
        header_map = dict(headers) if headers else None
        resolved = resilience or McpResilience()
        read_timeout = timedelta(seconds=resolved.timeout_seconds)
        client_info = Implementation(name=client_name, version=__version__)

        @asynccontextmanager
        async def factory() -> AsyncIterator[ClientSession]:
            async with streamablehttp_client(url, headers=header_map) as (read, write, _):
                async with ClientSession(
                    read, write, read_timeout_seconds=read_timeout, client_info=client_info
                ) as session:
                    await session.initialize()
                    yield session

        return cls(
            factory,
            tools=tools,
            resilience=resolved,
            client_name=client_name,
            requirements=requirements,
        )

    @classmethod
    def for_stdio(
        cls,
        *,
        command: str,
        args: Sequence[str],
        env: Mapping[str, str] | None = None,
        tools: Mapping[str, McpToolPolicy] | None = None,
        resilience: McpResilience | None = None,
        client_name: str = "arc",
        requirements: list[Requirement] | None = None,
    ) -> SdkMcpClient:
        """A client for a locally spawned MCP server, one process per session.

        Building the client only records the launch parameters — the process starts
        when the factory is entered, not here. ``env`` is passed straight to the SDK
        as the child's whole environment; the connector builder has already scrubbed
        it (REQ-273) and wrapped ``command``/``args`` with the deployment sandbox
        policy, so no host-steering variable and no unconfined argv reaches this seam.
        """
        parameters = StdioServerParameters(
            command=command, args=list(args), env=dict(env) if env is not None else None
        )
        resolved = resilience or McpResilience()
        read_timeout = timedelta(seconds=resolved.timeout_seconds)
        client_info = Implementation(name=client_name, version=__version__)

        @asynccontextmanager
        async def factory() -> AsyncIterator[ClientSession]:
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=read_timeout, client_info=client_info
                ) as session:
                    await session.initialize()
                    yield session

        return cls(
            factory,
            tools=tools,
            resilience=resolved,
            client_name=client_name,
            requirements=requirements,
        )

    # --- the hook contract ---------------------------------------------------

    def requirements(self) -> list[Requirement]:
        """What the host or the operator must supply, as injected at construction."""
        return list(self._requirements)

    async def probe(self) -> ProbeResult:
        """Prove the connection works and report the live tool list.

        Any SDK or transport failure is reported as an unreachable result rather
        than raised: probe is the one method the loader calls to decide reachability,
        so it must always answer.
        """
        try:
            tools = await self.describe_tools()
        except Exception as exc:
            # A degraded, readable result is the probe contract — probe answers
            # reachability for the loader and must never raise.
            return ProbeResult(reachable=False, detail=str(exc))
        return ProbeResult(reachable=True, tools=tools, detail=f"MCP SDK, {len(tools)} tool(s)")

    async def describe_tools(self) -> list[ToolSpec]:
        """The live tool list, classified by the manifest and by nothing else."""
        listed = await self._run("tools/list", lambda session: session.list_tools())
        specs: list[ToolSpec] = []
        for tool in listed.tools:
            spec = self._spec(tool)
            if _requires_header_mirroring(spec.input_schema):
                _logger.warning(
                    "excluding tool %r: it asks for a parameter to be mirrored into an "
                    "HTTP header, which this client does not do",
                    spec.name,
                )
                continue
            specs.append(spec)
        return specs

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Execute one call over the SDK and report what the server said about it.

        The wire is bounded here: a hung, retried, or repeatedly failing server
        raises :class:`~arcagent.extension.mcp_policy.UnavailableError` rather than
        hanging the turn or leaking a raw SDK/transport error (REL-05, LLM10). A
        server that *answers* with ``isError`` is a verdict, not a failure — the wire
        worked — so it returns an ERROR outcome and counts as a success for the breaker.
        """
        result = await self._run("tools/call", lambda session: session.call_tool(tool, args))
        content = _sdk_content(result)
        if result.isError:
            return ToolResult(
                tool=tool,
                outcome=ToolOutcome.ERROR,
                content=content or f"{tool} reported an error and returned no detail",
            )
        return ToolResult(
            tool=tool,
            outcome=ToolOutcome.OK,
            content=content or f"{tool} completed and returned no content",
        )

    async def _run(
        self,
        method: str,
        operation: Callable[[ClientSession], Awaitable[_T]],
    ) -> _T:
        """Drive one operation over a fresh session under the resilience bounds.

        Ports the old ``_exchange`` semantics onto the SDK's one-session-per-operation
        model: the breaker is checked before the wire is touched, each attempt is
        bounded by ``timeout_seconds`` (via :func:`asyncio.wait_for`, so the bound
        holds for *any* session implementation, not just one carrying the SDK's
        ``read_timeout_seconds``), a transport or protocol failure records a breaker
        failure and is retried up to ``max_attempts`` with exponential backoff, and a
        clean answer records success. Repeated failure or an already-open breaker
        raises :class:`~arcagent.extension.mcp_policy.UnavailableError`; no raw SDK,
        httpx, or anyio error escapes (REL-05).
        """
        retry_after = self._breaker.retry_after()
        if retry_after is not None:
            raise _UnavailableError(
                f"the MCP server is unavailable after repeated transport failures; "
                f"retry in {retry_after:.0f}s",
                {"method": method},
            )
        detail = ""
        for attempt in range(self._resilience.max_attempts):
            if attempt:
                await asyncio.sleep(self._resilience.backoff_seconds * 2 ** (attempt - 1))
            try:
                result = await asyncio.wait_for(
                    self._open_and_run(operation), self._resilience.timeout_seconds
                )
            except TimeoutError:
                self._breaker.record_failure()
                detail = f"no answer within {self._resilience.timeout_seconds:.0f}s"
            except Exception as exc:  # transport/protocol failure — mapped, never leaked
                self._breaker.record_failure()
                detail = str(exc)
            else:
                self._breaker.record_success()
                return result
        raise _UnavailableError(
            f"the MCP server did not complete {method} within "
            f"{self._resilience.max_attempts} attempt(s): {detail}",
            {"method": method},
        )

    async def _open_and_run(self, operation: Callable[[ClientSession], Awaitable[_T]]) -> _T:
        """Open one session and run the operation on it, closing it on the way out.

        Wrapped as a single coroutine so :func:`asyncio.wait_for` bounds the whole
        thing — factory entry (which initializes the SDK session) as well as the call.
        """
        async with self._session_factory() as session:
            return await operation(session)

    def _spec(self, tool: Tool) -> ToolSpec:
        """One served tool, classified from the manifest (REQ-269).

        ``tool.annotations`` is not read: the specification calls it untrusted, and
        it is the field a poisoned upstream would use to talk past the gate.
        """
        policy = self._tools.get(tool.name, _DEFAULT_POLICY)
        schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
        return ToolSpec(
            name=tool.name,
            description=tool.description or "",
            input_schema=schema,
            classification=policy.classification,
            capability_tags=list(policy.capability_tags),
        )


__all__ = [
    "McpResilience",
    "McpToolPolicy",
    "SdkMcpClient",
]
