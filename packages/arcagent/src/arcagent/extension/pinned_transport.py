"""An httpx transport that connects only to the address it just validated.

A hostname judged safe at add time can be re-pointed later (DNS rebinding), and a normal
HTTP client resolves the name again at connect time. This transport closes that gap: for
every request it resolves the host ONCE, judges EVERY address against the egress policy,
and connects to a validated address. The URL's host is swapped for that address, while
the ``Host`` header and the TLS server name (``sni_hostname``) stay the original name, so
virtual hosting and certificate verification still work.

It does this per request, and so again on every reconnect and every redirect hop: there
is no cached verdict for a rebinding server to outlive.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx

from arcagent.extension.egress_guard import (
    EgressPolicy,
    EgressRefusedError,
    IPAddress,
    parse_address,
)

_logger = logging.getLogger(__name__)

#: Called with ``(host, address, reason)`` when a connect is refused, so the caller can audit it.
RefusalSink = Callable[[str, str, str], None]

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, read=300.0)


class PinnedResolverTransport(httpx.AsyncBaseTransport):
    """Resolve, validate every answer, then connect to a validated address only."""

    def __init__(
        self,
        policy: EgressPolicy,
        *,
        inner: httpx.AsyncBaseTransport | None = None,
        on_refused: RefusalSink | None = None,
    ) -> None:
        self._policy = policy
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()
        self._on_refused = on_refused

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        address = await self._validated_address(host)
        pinned = httpx.Request(
            method=request.method,
            url=request.url.copy_with(host=str(address)),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def _validated_address(self, host: str) -> IPAddress:
        try:
            literal = parse_address(host)
        except ValueError:
            literal = None
        addresses = [literal] if literal is not None else await self._lookup(host)
        try:
            self._policy.check_all(host, addresses)
        except EgressRefusedError as exc:
            offender = next(a for a in addresses if self._policy.refusal(a) is not None)
            self._refuse(host, str(offender), str(exc))
            raise
        return addresses[0]

    async def _lookup(self, host: str) -> list[IPAddress]:
        try:
            answers = await asyncio.to_thread(self._policy.resolver, host)
            addresses = [parse_address(answer) for answer in answers]
        except (OSError, ValueError) as exc:
            self._refuse(host, "", f"lookup failed: {exc}")
            raise EgressRefusedError(f"{host} could not be resolved safely") from exc
        if not addresses:
            self._refuse(host, "", "lookup returned no address")
            raise EgressRefusedError(f"{host} did not resolve")
        return addresses

    def _refuse(self, host: str, address: str, reason: str) -> None:
        shown = address or "no address"
        _logger.warning("refused MCP connect to %s (%s): %s", host, shown, reason)
        if self._on_refused is not None:
            self._on_refused(host, address, reason)


def pinned_client_factory(
    policy: EgressPolicy, on_refused: RefusalSink | None
) -> Callable[..., httpx.AsyncClient]:
    """The ``httpx_client_factory`` the MCP SDK builds its HTTP client with.

    Mirrors the SDK's own defaults (redirects followed, 30s/300s timeouts) and changes
    only the transport; every redirect hop goes back through the same pinned lookup.
    """

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        options: dict[str, Any] = {
            "transport": PinnedResolverTransport(policy, on_refused=on_refused),
            "follow_redirects": True,
            "timeout": timeout if timeout is not None else _DEFAULT_TIMEOUT,
        }
        if headers is not None:
            options["headers"] = headers
        if auth is not None:
            options["auth"] = auth
        return httpx.AsyncClient(**options)

    return factory


__all__ = ["PinnedResolverTransport", "RefusalSink", "pinned_client_factory"]
