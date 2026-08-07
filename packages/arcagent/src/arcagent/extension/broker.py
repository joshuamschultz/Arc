"""SPEC-062 D-575 — the credential broker: a connector never receives the credential.

Every other design hands the credential to the connector and hopes: into a spawned
process's environment, or into a header a third-party attachment builds. An OX
Security scan found command injection in 43% of public MCP servers and path
traversal in 82%, so every upstream is presumed hostile — and a hostile upstream
holding a live token owns the account, not merely the call.

This module removes the credential from that blast radius. The broker resolves the
value, the connector is given a **handle** and a **loopback endpoint**, and the real
``Authorization`` header is attached here, on the way out, one function call before
the bytes leave the host. A connector that is fully compromised holds a token that
is useless anywhere except against the origin its grant was pinned to.

Four properties carry the guarantee, and each is load-bearing:

* **The pin is set at issue time, never at call time.** A broker attaches
  credentials, which makes it a confused deputy the moment the caller can choose the
  recipient. So the request target must be an origin-form path, the ``Host`` header
  the connector supplied is dropped, and redirects are not followed — a 302 to an
  attacker is handed back for the connector to think about, not walked with the
  credential in hand.
* **The value is resolved per call, never held.** :class:`CredentialBroker` keeps a
  coordinate, not a token. Rotation (see
  :mod:`arcagent.extension.credentials`) is therefore picked up on the next request
  instead of leaving a long-lived stdio connection wired to a consumed token, and
  deleting the secret stops the next call rather than the next grant.
* **Fail closed everywhere.** No credential in the store means no grant. No grant
  means no forward. A vanished credential means a 503. There is no branch in this
  file that answers a failure by handing the raw value over — that is the one
  fallback the component exists to eliminate.
* **The handle is not a credential.** 32 bytes from :func:`secrets.token_urlsafe`,
  meaningless off this host, scoped to one origin, and revocable in one call.

**On Linux, ``systemd LoadCredential=`` provides the storage half of this natively**:
the unit's credential is materialised into a private, non-swappable tmpfs readable
only by the unit and torn down when the unit stops, so it never lands on a shared
path and never outlives the service. It is complementary rather than redundant —
``LoadCredential=`` still delivers the value *to a process*, which is precisely what
this broker declines to do for third-party code. The two compose: swapping to it is
a :class:`~arcagent.extension.secrets.SecretBackend` reading
``$CREDENTIALS_DIRECTORY``, which is a change behind the store's existing seam and
touches nothing here. Nothing in this module depends on systemd, and the loopback
listener works identically on a laptop, in a container, and in a SCIF.

The residual exposure is stated plainly: the listener is bound to ``127.0.0.1``, so
any process on the host may open the port. The handle is what stops it, and a
stolen handle still cannot pick a recipient. A deployment wanting more binds the
agent's processes into their own network namespace; the endpoint contract does not
change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from secrets import token_urlsafe
from typing import Self
from urllib.parse import unquote, urlsplit

import httpx
from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.core.errors import ExtensionError
from arcagent.extension.secrets import SecretRef, SecretStore

_logger = logging.getLogger("arcagent.extension.broker")

#: The only interface the broker listens on.
LOOPBACK = "127.0.0.1"

#: Entropy in a handle. A handle authorizes use of a credential, so it is sized as a
#: bearer token rather than as an identifier.
HANDLE_BYTES = 32

#: Ceiling on one connector request head and one request body. A connector is
#: third-party code and an unbounded read is a memory-exhaustion primitive it would
#: otherwise get for free (LLM10).
MAX_HEAD_BYTES = 64 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024

#: Headers that describe one hop and must not be relayed to the next.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: Never sent upstream. ``host`` because the pinned URL decides the authority and a
#: forged one would arrive as the connector's choice; ``authorization`` because it
#: carried only the handle and is replaced here; the framing headers because the body
#: is re-framed.
_STRIPPED_FROM_REQUEST = _HOP_BY_HOP | {"host", "authorization", "content-length", "expect"}

#: Never returned to the connector: the answer's framing is rebuilt for it.
_STRIPPED_FROM_RESPONSE = _HOP_BY_HOP | {"content-length"}


@dataclass(frozen=True, repr=False)
class BrokerGrant:
    """What a connector is given in place of a credential.

    ``repr`` redacts the handle for the same reason
    :class:`~arcagent.extension.secrets.Secret` redacts its value: a grant
    interpolated into a log line must not print a live bearer token. Reaching the
    handle takes naming the attribute, which makes every deliberate use greppable.

    Attributes:
        handle: The opaque token the connector presents as its bearer credential.
        endpoint: The loopback origin the connector sends to, in place of ``upstream``.
        upstream: The one origin this grant can ever reach. Operator-readable.
    """

    handle: str
    endpoint: str
    upstream: str

    def __repr__(self) -> str:
        return f"BrokerGrant(endpoint={self.endpoint!r}, upstream={self.upstream!r}, handle=***)"


@dataclass(frozen=True)
class _Grant:
    """What the broker keeps: a coordinate and a pin — never a value."""

    ref: SecretRef
    upstream: str
    caller_did: str


@dataclass(frozen=True)
class _Request:
    """One parsed connector request."""

    method: str
    target: str
    headers: list[tuple[str, str]]
    body: bytes


@dataclass(frozen=True)
class _Response:
    """One answer to a connector, rendered as HTTP/1.1 with its own framing."""

    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def render(self) -> bytes:
        try:
            reason = HTTPStatus(self.status).phrase
        except ValueError:
            reason = "Unknown"
        lines = [f"HTTP/1.1 {self.status} {reason}"]
        lines += [f"{name}: {value}" for name, value in self.headers.items()]
        lines += [f"content-length: {len(self.body)}", "connection: close", "", ""]
        return "\r\n".join(lines).encode("latin-1", "replace") + self.body


class CredentialBroker:
    """Holds the credential, hands out handles, attaches the header on the way out.

    Args:
        secrets: Where credentials live. The broker reads through this seam and
            never stores a value of its own.
        sink: Audit sink. Grant, attach, and revoke are recorded with the secret's
            coordinate and the pinned upstream — never the value, never the handle.
        client: The HTTP client the broker owns and closes.
        timeout: Ceiling on one upstream request.
    """

    def __init__(
        self,
        secrets: SecretStore,
        *,
        sink: AuditSink | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._secrets = secrets
        self._sink = sink
        self._client = client or httpx.AsyncClient()
        self._timeout = timeout
        self._grants: dict[str, _Grant] = {}
        self._server: asyncio.Server | None = None
        self._port = 0

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @property
    def endpoint(self) -> str:
        """The loopback origin connectors are pointed at, in place of the upstream."""
        if self._server is None:
            raise ExtensionError(
                code="BROKER_NOT_STARTED",
                message="the credential broker is not listening; start it before issuing a grant",
            )
        return f"http://{LOOPBACK}:{self._port}"

    async def start(self) -> None:
        """Bind the loopback listener on an ephemeral port."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._serve, LOOPBACK, 0, limit=MAX_HEAD_BYTES)
        self._port = int(self._server.sockets[0].getsockname()[1])

    async def aclose(self) -> None:
        """Revoke every grant and stop listening. Nothing outlives the broker."""
        self._grants.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self._client.aclose()

    async def issue(self, ref: SecretRef, *, upstream: str, caller_did: str) -> BrokerGrant:
        """Pin one credential to one origin and return the handle that reaches it.

        The credential is resolved once here so a connection that was never
        authorized fails now rather than at the first call, and the resolved value
        is discarded immediately — each request resolves its own.

        Raises:
            ExtensionError: The broker is not listening, the upstream is not an
                absolute http(s) origin, or no credential is stored at ``ref``.
        """
        endpoint = self.endpoint
        origin = _pin(upstream)
        if await self._secrets.get(ref, caller_did=caller_did) is None:
            self._audit("credential.broker_issue", ref, caller_did, "not_found", origin)
            raise ExtensionError(
                code="BROKER_CREDENTIAL_MISSING",
                message=f"no stored credential for {ref}; authorize the connection first",
                details={"secret": str(ref), "upstream": origin},
            )
        handle = token_urlsafe(HANDLE_BYTES)
        self._grants[handle] = _Grant(ref=ref, upstream=origin, caller_did=caller_did)
        self._audit("credential.broker_issue", ref, caller_did, "allow", origin)
        return BrokerGrant(handle=handle, endpoint=endpoint, upstream=origin)

    async def revoke(self, handle: str) -> bool:
        """Forget one grant. True when one was live."""
        grant = self._grants.pop(handle, None)
        if grant is None:
            return False
        self._audit(
            "credential.broker_revoke", grant.ref, grant.caller_did, "allow", grant.upstream
        )
        return True

    # --- serving -------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer one connector request. One connection, one exchange."""
        try:
            response = await self._respond(reader)
        except Exception:
            # A failure serving one connector must not take the listener down with
            # it. The message is static so a traceback cannot carry a credential.
            _logger.exception("the credential broker failed to serve a request")
            response = _Response(500, b"broker error")
        try:
            writer.write(response.render())
            await writer.drain()
        except OSError as exc:
            _logger.debug("a connector closed before reading the broker's answer: %s", exc)
        finally:
            writer.close()

    async def _respond(self, reader: asyncio.StreamReader) -> _Response:
        """Authenticate the handle, then forward under its pin."""
        request = await _read_request(reader)
        if request is None:
            return _Response(400, b"malformed or oversized request")
        handle = _presented_handle(request.headers)
        grant = self._grants.get(handle) if handle else None
        if grant is None:
            # A log line rather than an audit event: there is no identified actor to
            # attribute one to, and anything reachable without a handle must not be
            # able to drive unbounded writes into the audit store.
            _logger.warning("refusing a broker request: no live grant for the presented handle")
            return _Response(401, b"unknown or revoked handle")
        return await self._forward(grant, request)

    async def _forward(self, grant: _Grant, request: _Request) -> _Response:
        """Attach the real credential and send it to the pinned origin, only."""
        if not _within_pin(request.target):
            _logger.warning("refusing a broker request whose target escapes the pinned upstream")
            self._attach_audit(grant, "deny")
            return _Response(400, b"request target must be an origin-form path inside the pin")

        secret = await self._secrets.get(grant.ref, caller_did=grant.caller_did)
        if secret is None:
            _logger.warning("refusing a broker request: the credential is no longer stored")
            self._attach_audit(grant, "not_found")
            return _Response(503, b"the credential for this connection is no longer available")

        headers = _forwardable(request.headers)
        headers["authorization"] = f"Bearer {secret.reveal()}"
        try:
            answer = await self._client.request(
                request.method,
                grant.upstream + request.target,
                headers=headers,
                content=request.body,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            _logger.warning("the broker could not reach %s: %s", grant.upstream, exc)
            self._attach_audit(grant, "error")
            return _Response(502, b"the upstream could not be reached")
        self._attach_audit(grant, "allow")
        return _Response(answer.status_code, answer.content, _returnable(answer.headers))

    # --- audit ---------------------------------------------------------------

    def _attach_audit(self, grant: _Grant, outcome: str) -> None:
        self._audit(
            "credential.broker_attach", grant.ref, grant.caller_did, outcome, grant.upstream
        )

    def _audit(
        self, action: str, ref: SecretRef, caller_did: str, outcome: str, upstream: str
    ) -> None:
        """Record which credential went where, for whom — never the value or handle."""
        if self._sink is None:
            return
        emit(
            AuditEvent(
                actor_did=caller_did,
                action=action,
                target=f"secret:{ref}",
                outcome=outcome,
                extra={"upstream": upstream},
            ),
            self._sink,
        )


def _pin(upstream: str) -> str:
    """Normalise the one origin a grant may ever reach.

    Everything a connector later asks for hangs off this string, so anything that is
    not an absolute http(s) origin is refused rather than guessed at.
    """
    parts = urlsplit(upstream)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
        raise ExtensionError(
            code="BROKER_UPSTREAM_INVALID",
            message=f"a broker upstream must be an absolute http(s) origin, not {upstream!r}",
            details={"upstream": upstream},
        )
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _within_pin(target: str) -> bool:
    """True when a target is an origin-form path that cannot climb out of the pin.

    Two ways out, both closed here. An absolute or protocol-relative target names its
    own host, which would let the connector choose who receives the credential. And a
    ``..`` segment walks out of a pin narrowed to a path prefix — the pin is the whole
    least-privilege story, so traversal against it is traversal against the boundary.
    """
    if not target.startswith("/") or target.startswith("//"):
        return False
    path = target.partition("?")[0].partition("#")[0]
    # Unquoted first: ``%2e%2e`` is the same climb, spelled to slip a literal check.
    return ".." not in unquote(path).split("/")


async def _read_request(reader: asyncio.StreamReader) -> _Request | None:
    """Parse one bounded HTTP/1.1 request, or ``None`` when it is not one.

    Chunked framing is refused rather than ignored: reading it as a bodyless request
    would silently send a truncated call upstream with a live credential on it.
    """
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
        return None

    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return None

    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        # An obs-fold continuation has no colon of its own; dropping it would change
        # the header it belongs to, so the whole request is refused instead.
        if not separator or not name.strip() or line[0].isspace():
            return None
        headers.append((name.strip().lower(), value.strip()))
    if any(name == "transfer-encoding" for name, _ in headers):
        return None

    length = _content_length(headers)
    if length is None:
        return None
    try:
        body = await reader.readexactly(length) if length else b""
    except asyncio.IncompleteReadError:
        return None
    return _Request(method=parts[0], target=parts[1], headers=headers, body=body)


def _content_length(headers: list[tuple[str, str]]) -> int | None:
    """The declared body length, or ``None`` when it is unusable.

    Two ``content-length`` headers is a request-smuggling shape, not an ambiguity to
    resolve, so it is refused.
    """
    declared = [value for name, value in headers if name == "content-length"]
    if not declared:
        return 0
    if len(declared) > 1:
        return None
    try:
        length = int(declared[0])
    except ValueError:
        return None
    return length if 0 <= length <= MAX_BODY_BYTES else None


def _presented_handle(headers: list[tuple[str, str]]) -> str:
    """The handle the connector presented, from the header it authenticates with."""
    for name, value in headers:
        if name == "authorization":
            scheme, separator, token = value.partition(" ")
            if separator and scheme.lower() == "bearer":
                return token.strip()
    return ""


def _forwardable(headers: list[tuple[str, str]]) -> dict[str, str]:
    """The connector's headers, minus the ones this hop owns.

    A dict, so a repeated header collapses to its last value rather than reaching the
    upstream twice — the shape a request-smuggling attempt needs.
    """
    return {
        name: value
        for name, value in headers
        if name not in _STRIPPED_FROM_REQUEST and _is_safe(name) and _is_safe(value)
    }


def _returnable(headers: httpx.Headers) -> dict[str, str]:
    """The upstream's headers, minus framing — and minus anything that could split."""
    return {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() not in _STRIPPED_FROM_RESPONSE and _is_safe(name) and _is_safe(value)
    }


def _is_safe(text: str) -> bool:
    """True when a header name or value cannot forge or split a second header."""
    return not any(character in text for character in "\r\n\x00")


__all__ = [
    "HANDLE_BYTES",
    "LOOPBACK",
    "MAX_BODY_BYTES",
    "MAX_HEAD_BYTES",
    "BrokerGrant",
    "CredentialBroker",
]
