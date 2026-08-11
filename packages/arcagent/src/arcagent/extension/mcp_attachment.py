"""McpAttachment — MCP as one implementation of the hook (SPEC-062 COMP-005, REQ-279).

D-569 makes a vetted CLI the default attachment and this the *option*, so the value
of this module is as much proof as capability: a wire protocol with its own
discovery, its own error taxonomy, and two transports reaches the agent through the
same four unchanged methods of
:class:`~arcagent.extension.attachment.ExtensionAttachment` that a local binary
does. Nothing in ``arcagent`` outside this file knows the protocol exists.

The target is the **2026-07-28 stateless revision**, and its deletions shape the
code more than its additions:

* **No handshake, no session.** Every request carries its own protocol version and
  capabilities in ``params._meta``; the server infers nothing from the connection.
  A client that silently fell back to the pre-stateless handshake would end up
  speaking legacy semantics to a server that never checked for one, so this module
  has no fallback at all: :meth:`McpAttachment.probe` uses ``server/discover`` to
  tell the eras apart and refuses the older one by name.
* **Two error tiers that must never merge.** A JSON-RPC ``error`` object means the
  request was wrong — there is no tool outcome, so it raises (REQ-271). A success
  envelope with ``isError: true`` is the tool reporting a real, actionable answer,
  and it reaches the agent as a readable
  :class:`~arcagent.extension.attachment.ToolResult` to self-correct from (REQ-270).
* **``resultType``.** ``input_required`` is a half-finished round trip carrying no
  content anyone may act on. Reading it as completion is a silent wrong answer, so
  it maps to its own outcome; an unrecognised value is invalid and raises.

Two security positions are deliberate. Server-supplied ``annotations`` are ignored
entirely — the specification calls them untrusted, and ``classification`` and
``capability_tags`` are what feed the trifecta gate, so they come from the manifest
via :class:`McpToolPolicy` and default to the restrictive value (REQ-269). And a
spawned server is started only through
:class:`~arcagent.extension.launcher.ProcessLauncher`, which is what applies
environment scrubbing, artifact pinning, and sandbox policy; there is no second
spawn path in this file to audit.

No vendor SDK (CON-7): the wire is httpx and the standard library, so every byte
sent on an agent's behalf is readable here.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

import httpx

from arcagent import __version__
from arcagent.core.errors import ExtensionError
from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    RequirementKind,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.launcher import ProcessDefinition, ProcessLauncher
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
from arcagent.extension.secrets import Secret

_logger = logging.getLogger(__name__)

#: The revision this client speaks. Sent on every request; a server that cannot
#: honour it says so through discovery rather than by misreading a later call.
PROTOCOL_VERSION = "2026-07-28"

#: Ceiling on one inbound message. A server is third-party code and an unbounded
#: read is a memory-exhaustion primitive it gets for free (LLM10).
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
_META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"

#: JSON-RPC code a modern server returns when it speaks MCP but not this revision.
#: Distinguishing it from every other error is what keeps a version mismatch from
#: being misdiagnosed as a server predating the stateless revision.
_UNSUPPORTED_PROTOCOL_VERSION = -32022

#: Marks a tool parameter the server wants mirrored into an HTTP header. This
#: client does not mirror, so such a tool is excluded rather than called wrongly.
_HEADER_MIRROR_KEY = "x-mcp-header"

#: Pages of tools to follow before concluding the server is not terminating.
_MAX_TOOL_PAGES = 50

_READ_CHUNK_BYTES = 64 * 1024

#: The sentinel that carries a header value which is not plain printable ASCII.
_BASE64_PREFIX = "=?base64?"
_BASE64_SUFFIX = "?="


@runtime_checkable
class McpTransport(Protocol):
    """How one JSON-RPC message gets to a server and its answer comes back.

    Both shipped transports return the response *envelope* unread — including a
    JSON-RPC ``error`` body, which is the server speaking rather than the wire
    failing. Only a genuine transport failure raises here.
    """

    def requirements(self) -> list[Requirement]:
        """What the host or the operator must supply before this can connect."""
        ...

    async def send(self, message: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        """Send one request and return the matching JSON-RPC response envelope."""
        ...

    async def close(self) -> None:
        """Release whatever this transport is holding open."""
        ...


def _transport_failure(message: str, **details: Any) -> ExtensionError:
    """The wire itself failed: nothing answered, so there is no result to read."""
    return ExtensionError(
        code="EXTENSION_TRANSPORT_FAILED", message=message, details=dict(details)
    )


def _protocol_failure(message: str, **details: Any) -> ExtensionError:
    """The server answered, but not in a shape this revision permits."""
    return ExtensionError(code="MCP_PROTOCOL_ERROR", message=message, details=dict(details))


def _header_value(value: str) -> str:
    """Encode a mirrored value so it cannot forge or split a header.

    Tool names are only *recommended* to be header-safe, so a name carrying a
    newline would otherwise inject a header of the server's choosing. The
    specification's base64 sentinel covers exactly this, and applies to a
    plain-ASCII value that merely looks like the sentinel.
    """
    printable = value.isascii() and all(0x20 <= ord(character) <= 0x7E for character in value)
    sentinel = value.startswith(_BASE64_PREFIX) and value.endswith(_BASE64_SUFFIX)
    if printable and value == value.strip() and not sentinel:
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"{_BASE64_PREFIX}{encoded}{_BASE64_SUFFIX}"


class HttpTransport:
    """Streamable HTTP: one POST per message to a single endpoint.

    The server chooses per request whether to answer with a JSON object or an
    event stream, so the client advertises both and reads whichever arrives. This
    revision has no standalone stream and no resumption, which is why nothing here
    holds state between requests.

    Args:
        url: The server's single MCP endpoint.
        client: The HTTP client this transport owns and closes.
        token: Bearer credential, revealed only when a request is built.
        credential_field: The secret coordinate an operator must supply, declared
            as a requirement so ``connector add`` can prompt for it.
    """

    def __init__(
        self,
        *,
        url: str,
        client: httpx.AsyncClient,
        token: Secret | None = None,
        credential_field: str = "",
    ) -> None:
        self._url = url
        self._client = client
        self._token = token
        self._credential_field = credential_field

    def requirements(self) -> list[Requirement]:
        """A hosted server needs no binary — only the credential to reach it."""
        if not self._credential_field:
            return []
        return [
            Requirement(
                kind=RequirementKind.CREDENTIAL,
                name=self._credential_field,
                instruction=f"Provide the credential for {self._url}",
            )
        ]

    async def send(self, message: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        body = json.dumps(message).encode("utf-8")
        try:
            async with self._client.stream(
                "POST", self._url, headers=self._headers(message), content=body, timeout=timeout
            ) as response:
                return await self._read(response, message["id"])
        except httpx.TimeoutException as exc:
            raise TimeoutError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise _transport_failure(
                f"the MCP server at {self._url} could not be reached: {exc}", url=self._url
            ) from exc

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self, message: dict[str, Any]) -> dict[str, str]:
        """Mirror the routing fields intermediaries are entitled to read.

        The mirrored version must equal the one in the body or a conforming server
        rejects the request, so both are read from the same place.
        """
        params = message.get("params") or {}
        meta = params.get("_meta") or {}
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": str(meta.get(_META_PROTOCOL_VERSION, PROTOCOL_VERSION)),
            "mcp-method": str(message["method"]),
        }
        name = params.get("name") or params.get("uri")
        if isinstance(name, str) and name:
            headers["mcp-name"] = _header_value(name)
        if self._token is not None:
            headers["authorization"] = f"Bearer {self._token.reveal()}"
        return headers

    async def _read(self, response: httpx.Response, request_id: int) -> dict[str, Any]:
        """Return the envelope, whichever of the two answer shapes arrived.

        A 4xx is not automatically a transport failure: a modern server uses one to
        carry its own JSON-RPC error, and treating that as a hang-up would hide the
        very message that says which versions it does support.
        """
        media = response.headers.get("content-type", "")
        if media.startswith("text/event-stream"):
            return await self._read_events(response, request_id)
        envelope = self._decode(await self._read_body(response))
        if envelope is None:
            raise _transport_failure(
                f"the MCP server at {self._url} answered HTTP {response.status_code} "
                f"with a body that is not a JSON-RPC message",
                url=self._url,
                status=response.status_code,
            )
        return envelope

    async def _read_body(self, response: httpx.Response) -> str:
        """Buffer the body, refusing to grow past the ceiling.

        Bounded while reading rather than after: a check on an already-buffered
        body is a guard that runs once the damage is done.
        """
        chunks: list[bytes] = []
        received = 0
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > MAX_MESSAGE_BYTES:
                raise _transport_failure(
                    f"the MCP server at {self._url} returned more than {MAX_MESSAGE_BYTES} bytes",
                    url=self._url,
                )
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    async def _read_events(self, response: httpx.Response, request_id: int) -> dict[str, Any]:
        """Take the response out of the stream, skipping what precedes it.

        Notifications and keep-alive comments may arrive first; only the message
        carrying this request's id is the answer.
        """
        received = 0
        async for line in response.aiter_lines():
            received += len(line)
            if received > MAX_MESSAGE_BYTES:
                raise _transport_failure(
                    f"the MCP server at {self._url} streamed more than "
                    f"{MAX_MESSAGE_BYTES} bytes without answering",
                    url=self._url,
                )
            if not line.startswith("data:"):
                continue
            envelope = self._decode(line[len("data:") :])
            if envelope is not None and envelope.get("id") == request_id:
                return envelope
        raise _transport_failure(
            f"the MCP server at {self._url} closed the stream without answering", url=self._url
        )

    @staticmethod
    def _decode(text: str) -> dict[str, Any] | None:
        """Parse one JSON-RPC message, or ``None`` when the text is not one."""
        try:
            envelope = json.loads(text)
        except ValueError:
            return None
        return envelope if isinstance(envelope, dict) else None


class StdioTransport:
    """A locally spawned server, one newline-delimited JSON-RPC message per line.

    The process is started through :class:`~arcagent.extension.launcher.ProcessLauncher`
    and never here, so environment scrubbing, artifact pinning, and sandbox policy
    apply to it exactly as they do to every other extension process (REQ-292).

    Two properties of the channel drive the implementation. It is *shared* — one
    stdout carries responses to every in-flight request — so requests are
    serialised and replies are matched by id, and anything else on the channel is
    skipped rather than misread. And a message is *not* a line in the stream
    reader's sense: framing is done over raw chunks here because the reader's
    default line ceiling is 64 KiB, which a real tool list passes without effort.

    ``stderr`` is drained and logged, never inspected for meaning: the
    specification says a server may write there freely and a client must not read
    it as failure. Draining is not cosmetic — an undrained pipe fills and stops
    the server dead.
    """

    def __init__(
        self,
        *,
        launcher: ProcessLauncher,
        definition: ProcessDefinition,
        install_instruction: str = "",
    ) -> None:
        self._launcher = launcher
        self._definition = definition
        self._install_instruction = install_instruction
        self._lock = asyncio.Lock()
        self._attached: object | None = None
        self._buffer = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None

    def requirements(self) -> list[Requirement]:
        """The server's own command is a host prerequisite: Arc directs, the operator installs."""
        return [
            Requirement(
                kind=RequirementKind.HOST,
                name=self._definition.argv[0],
                instruction=self._install_instruction,
            )
        ]

    async def send(self, message: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        async with self._lock:
            handle = await self._launcher.acquire(self._definition)
            self._attach(handle.process)
            return await asyncio.wait_for(self._exchange(handle.process, message), timeout)

    async def close(self) -> None:
        """Stop draining. The launcher owns the process and reaps it."""
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        self._attached = None
        self._buffer.clear()

    def _attach(self, process: asyncio.subprocess.Process) -> None:
        """Bind to a process, discarding anything held for a previous one.

        A restarted server means the buffered tail belongs to a dead process;
        parsing it into the next reply would splice two conversations together.
        """
        if process is self._attached:
            return
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        self._attached = process
        self._buffer.clear()
        self._stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))

    async def _exchange(
        self, process: asyncio.subprocess.Process, message: dict[str, Any]
    ) -> dict[str, Any]:
        """Write one request, then read until this request's answer arrives."""
        if process.stdin is None or process.stdout is None:
            raise _transport_failure(
                f"{self._definition.argv[0]} was started without usable standard streams",
                key=self._definition.key,
            )
        # json.dumps escapes any newline inside a value, so one message is one line
        # by construction rather than by sanitising the caller's arguments.
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await process.stdin.drain()
        while True:
            line = await self._read_message(process.stdout)
            envelope = self._decode(line)
            if envelope is not None and envelope.get("id") == message["id"]:
                return envelope

    async def _read_message(self, reader: asyncio.StreamReader) -> str:
        """Read one newline-delimited message, bounded, over raw chunks."""
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                return line.decode("utf-8", "replace")
            if len(self._buffer) > MAX_MESSAGE_BYTES:
                raise _transport_failure(
                    f"{self._definition.argv[0]} sent more than {MAX_MESSAGE_BYTES} bytes "
                    f"without completing a message",
                    key=self._definition.key,
                )
            chunk = await reader.read(_READ_CHUNK_BYTES)
            if not chunk:
                raise _transport_failure(
                    f"{self._definition.argv[0]} closed its output without answering",
                    key=self._definition.key,
                )
            self._buffer += chunk

    def _decode(self, line: str) -> dict[str, Any] | None:
        """Parse one line, tolerating the banner a server should not have written.

        Refusing the whole connection over a stray line would break a server that
        is otherwise working, so the line is reported and skipped; the warning is
        what makes the violation visible.
        """
        if not line.strip():
            return None
        try:
            envelope = json.loads(line)
        except ValueError:
            _logger.warning(
                "%s wrote a non-MCP line to stdout: %.200s", self._definition.argv[0], line
            )
            return None
        return envelope if isinstance(envelope, dict) else None

    async def _drain_stderr(self, reader: asyncio.StreamReader | None) -> None:
        """Keep the pipe empty and the output visible. Never a failure signal."""
        if reader is None:
            return
        while chunk := await reader.read(_READ_CHUNK_BYTES):
            _logger.debug(
                "%s stderr: %s",
                self._definition.argv[0],
                chunk.decode("utf-8", "replace").rstrip(),
            )


def _requires_header_mirroring(node: object) -> bool:
    """True when a schema asks for a parameter to be mirrored into a header."""
    if isinstance(node, dict):
        if _HEADER_MIRROR_KEY in node:
            return True
        return any(_requires_header_mirroring(value) for value in node.values())
    if isinstance(node, list):
        return any(_requires_header_mirroring(item) for item in node)
    return False


class McpAttachment:
    """Attaches an external system by speaking MCP to it over a supplied transport.

    Satisfies :class:`~arcagent.extension.attachment.ExtensionAttachment`, so an
    MCP connector reaches the agent through exactly the same four methods as every
    other kind of attachment — which is the whole claim this class exists to make.

    Args:
        transport: How messages reach the server. Swapping stdio for HTTP changes
            nothing else about this class, including its requirement list.
        tools: Per-tool manifest declarations. Anything absent gets the restrictive
            default; nothing here is ever taken from the server.
        resilience: Timeout, retry, and circuit-breaker bounds.
        client_name: Identity reported to the server, for its logs and ours.
    """

    def __init__(
        self,
        transport: McpTransport,
        *,
        tools: Mapping[str, McpToolPolicy] | None = None,
        resilience: McpResilience | None = None,
        client_name: str = "arc",
    ) -> None:
        self._transport = transport
        self._tools = dict(tools or {})
        self._resilience = resilience or McpResilience()
        self._client_name = client_name
        self._ids = itertools.count(1)
        self._breaker = _CircuitBreaker(
            self._resilience.failure_threshold, self._resilience.reset_after_seconds
        )

    # --- the hook contract ---------------------------------------------------

    def requirements(self) -> list[Requirement]:
        """Whatever the transport needs — a binary to spawn, or a credential."""
        return self._transport.requirements()

    async def probe(self) -> ProbeResult:
        """Prove the connection works, and refuse a server from the older era.

        Discovery is the probe because it is the one request whose answer
        distinguishes a modern server from one still expecting a handshake. A
        client that fell back would speak this revision's ``tools/call`` to a
        server reading it under legacy rules; refusing is the deterministic
        outcome, so nothing here has a fallback to take.
        """
        try:
            envelope = await self._exchange(self._message("server/discover", {}))
        except ExtensionError as exc:
            return ProbeResult(reachable=False, detail=exc.message)

        error = envelope.get("error")
        if isinstance(error, dict):
            return ProbeResult(reachable=False, detail=self._refusal(error))
        try:
            tools = await self.describe_tools()
        except ExtensionError as exc:
            return ProbeResult(reachable=False, detail=exc.message)
        return ProbeResult(
            reachable=True, tools=tools, detail=f"MCP {PROTOCOL_VERSION}, {len(tools)} tool(s)"
        )

    async def describe_tools(self) -> list[ToolSpec]:
        """The live tool list, classified by the manifest and by nothing else."""
        specs: list[ToolSpec] = []
        for tool in await self._list_tools():
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
        """Execute one call and report what the server said about it."""
        message = self._message("tools/call", {"name": tool, "arguments": args})
        try:
            envelope = await self._exchange(message)
        except _UnavailableError as exc:
            return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=exc.message)
        return self._outcome(tool, self._result(envelope))

    # --- requests ------------------------------------------------------------

    def _message(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One JSON-RPC request, carrying everything the server needs to serve it.

        ``_meta`` is not decoration: the revision is stateless, so a request
        missing its protocol version or capabilities is malformed and refused.
        """
        return {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": {
                **params,
                "_meta": {
                    _META_PROTOCOL_VERSION: PROTOCOL_VERSION,
                    _META_CLIENT_INFO: {"name": self._client_name, "version": __version__},
                    _META_CLIENT_CAPABILITIES: {},
                },
            },
        }

    async def _exchange(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send with a bounded retry and a breaker, and return what came back.

        A server that answers with a JSON-RPC error has a working connection and
        is reporting a verdict, so it counts as a success here; only silence does
        not.
        """
        retry_after = self._breaker.retry_after()
        if retry_after is not None:
            raise _UnavailableError(
                f"the MCP server is unavailable after repeated transport failures; "
                f"retry in {retry_after:.0f}s",
                {"method": message["method"]},
            )
        for attempt in range(self._resilience.max_attempts):
            if attempt:
                await asyncio.sleep(self._resilience.backoff_seconds * 2 ** (attempt - 1))
            try:
                envelope = await self._transport.send(
                    message, timeout=self._resilience.timeout_seconds
                )
            except TimeoutError:
                self._breaker.record_failure()
            except ExtensionError:
                self._breaker.record_failure()
                raise
            else:
                self._breaker.record_success()
                return envelope
        raise _UnavailableError(
            f"the MCP server did not answer {message['method']} within "
            f"{self._resilience.timeout_seconds:.0f}s",
            {"method": message["method"]},
        )

    async def _list_tools(self) -> list[dict[str, Any]]:
        """Every page of ``tools/list``. A short list looks like missing verbs."""
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(_MAX_TOOL_PAGES):
            params = {"cursor": cursor} if cursor is not None else {}
            result = self._result(await self._exchange(self._message("tools/list", params)))
            served = result.get("tools", [])
            if not isinstance(served, list):
                raise _protocol_failure("the MCP server returned a tool list that is not a list")
            tools.extend(item for item in served if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                return tools
            if cursor in seen:
                raise _protocol_failure(
                    "the MCP server repeated a tool-list cursor, which would never end",
                    cursor=cursor,
                )
            seen.add(cursor)
        raise _protocol_failure(
            f"the MCP server served more than {_MAX_TOOL_PAGES} pages of tools"
        )

    # --- results -------------------------------------------------------------

    @staticmethod
    def _result(envelope: dict[str, Any]) -> dict[str, Any]:
        """Unwrap a response, raising on the tier that is a protocol failure."""
        error = envelope.get("error")
        if isinstance(error, dict):
            raise _protocol_failure(
                f"the MCP server rejected the request: "
                f"{error.get('message', 'no message')} ({error.get('code')})",
                code=error.get("code"),
            )
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise _protocol_failure("the MCP server answered with neither a result nor an error")
        return result

    def _spec(self, tool: dict[str, Any]) -> ToolSpec:
        """One served tool, classified from the manifest (REQ-269).

        ``annotations`` is not read. The specification calls it untrusted, and it
        is the field a poisoned upstream would use to talk its way past the gate.
        """
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise _protocol_failure("the MCP server served a tool with no name")
        schema = tool.get("inputSchema")
        description = tool.get("description")
        policy = self._tools.get(name, _DEFAULT_POLICY)
        return ToolSpec(
            name=name,
            description=description if isinstance(description, str) else "",
            input_schema=schema if isinstance(schema, dict) else {},
            classification=policy.classification,
            capability_tags=list(policy.capability_tags),
        )

    @staticmethod
    def _outcome(tool: str, result: dict[str, Any]) -> ToolResult:
        """Map one completed exchange onto the outcome the agent acts on."""
        kind = result.get("resultType", "complete")
        if kind == "input_required":
            # Carries no content anyone may act on — only what is still needed.
            return ToolResult(
                tool=tool,
                outcome=ToolOutcome.INPUT_REQUIRED,
                content=json.dumps(
                    {
                        "inputRequests": result.get("inputRequests", {}),
                        "requestState": result.get("requestState"),
                    }
                ),
            )
        if kind != "complete":
            raise _protocol_failure(
                f"the MCP server returned an unrecognised result type {kind!r}", result_type=kind
            )
        content = _content_of(result)
        if result.get("isError"):
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

    def _refusal(self, error: dict[str, Any]) -> str:
        """Explain a failed discovery in terms the operator can act on."""
        if error.get("code") == _UNSUPPORTED_PROTOCOL_VERSION:
            data = error.get("data")
            supported = data.get("supported", []) if isinstance(data, dict) else []
            named = ", ".join(str(version) for version in supported) or "nothing named"
            return f"the server does not support MCP {PROTOCOL_VERSION}; it supports {named}"
        return (
            f"the server did not answer discovery ({error.get('message', 'no message')}), "
            f"so it predates the stateless revision; this client speaks only "
            f"MCP {PROTOCOL_VERSION}"
        )


def _content_of(result: dict[str, Any]) -> str:
    """The part of a result the agent reads.

    Structured content wins when offered: it is the schema-conforming form, and the
    text block beside it is a serialisation of the same thing.
    """
    if "structuredContent" in result:
        return json.dumps(result["structuredContent"])
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            # A non-text block still happened; dropping it silently would shorten
            # the answer without saying so.
            parts.append(f"[{block.get('type', 'unknown')}]")
    return "\n".join(part for part in parts if part)


__all__ = [
    "MAX_MESSAGE_BYTES",
    "PROTOCOL_VERSION",
    "HttpTransport",
    "McpAttachment",
    "McpResilience",
    "McpToolPolicy",
    "McpTransport",
    "StdioTransport",
]
