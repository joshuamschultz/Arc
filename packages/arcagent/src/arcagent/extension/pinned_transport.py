"""An httpx transport that connects only to the address it just validated.

A hostname judged safe at add time can be re-pointed later (DNS rebinding), and a normal
HTTP client resolves the name again at connect time. This transport closes that gap: for
every request it resolves the host ONCE, judges EVERY address against the egress policy,
and connects to a validated address. The URL's host is swapped for that address, while
the ``Host`` header and the TLS server name (``sni_hostname``) stay the original name, so
virtual hosting and certificate verification still work.

It does this per request, and so again on every reconnect and every redirect hop: there
is no cached verdict for a rebinding server to outlive.

Proxy trade-off. Behind a corporate proxy the client cannot resolve the destination
itself: the proxy does, so a name's address is not ours to judge or pin. When proxying is
allowed (the operator's ``[tools.policy] mcp_via_proxy = true`` above personal tier;
personal tier honors the environment's proxy without it) a request to a name is forwarded
to the proxy as-is. A literal address is still judged first, and hosts matched by
``NO_PROXY`` still take the pinned direct path. The DNS-rebinding guarantee is therefore
traded for the proxy's own egress controls, which the operator opts into explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.request
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

Bypass = Callable[[str], bool]


def proxy_from_environment() -> tuple[str | None, Bypass]:
    """The HTTPS proxy URL (``HTTPS_PROXY``/``ALL_PROXY``) and a ``NO_PROXY`` matcher."""
    proxies = urllib.request.getproxies_environment()
    url = proxies.get("https") or proxies.get("all")

    entries = [
        e.strip().lower().lstrip("*") for e in proxies.get("no", "").split(",") if e.strip()
    ]

    def bypass(host: str) -> bool:
        name = host.lower()
        return any(
            entry == "*"
            or name == entry.lstrip(".")
            or name.endswith(entry if entry.startswith(".") else f".{entry}")
            for entry in entries
        )

    return url, bypass


class PinnedResolverTransport(httpx.AsyncBaseTransport):
    """Resolve, validate every answer, then connect to a validated address only."""

    def __init__(
        self,
        policy: EgressPolicy,
        *,
        inner: httpx.AsyncBaseTransport | None = None,
        on_refused: RefusalSink | None = None,
        proxy: httpx.AsyncBaseTransport | None = None,
        bypass: Bypass | None = None,
    ) -> None:
        self._policy = policy
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()
        self._on_refused = on_refused
        self._proxy = proxy if policy.proxy_allowed else None
        self._bypass = bypass or (lambda _host: False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if self._proxy is not None and not self._bypass(host):
            return await self._via_proxy(self._proxy, request, host)
        address = await self._validated_address(host)
        pinned = httpx.Request(
            method=request.method,
            url=request.url.copy_with(host=str(address)),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return await self._inner.handle_async_request(pinned)

    async def _via_proxy(
        self, proxy: httpx.AsyncBaseTransport, request: httpx.Request, host: str
    ) -> httpx.Response:
        """Forward to the proxy; only a literal address can be judged here."""
        try:
            literal = parse_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            self._judge(host, [literal])
        return await proxy.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
        if self._proxy is not None:
            await self._proxy.aclose()

    async def _validated_address(self, host: str) -> IPAddress:
        try:
            literal = parse_address(host)
        except ValueError:
            literal = None
        addresses = [literal] if literal is not None else await self._lookup(host)
        self._judge(host, addresses)
        return addresses[0]

    def _judge(self, host: str, addresses: list[IPAddress]) -> None:
        try:
            self._policy.check_all(host, addresses)
        except EgressRefusedError as exc:
            offender = next(a for a in addresses if self._policy.refusal(a) is not None)
            self._refuse(host, str(offender), str(exc))
            raise

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

    proxy_url, bypass = proxy_from_environment() if policy.proxy_allowed else (None, None)

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        options: dict[str, Any] = {
            "transport": PinnedResolverTransport(
                policy,
                on_refused=on_refused,
                proxy=httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None,
                bypass=bypass,
            ),
            "follow_redirects": True,
            "timeout": timeout if timeout is not None else _DEFAULT_TIMEOUT,
        }
        if headers is not None:
            options["headers"] = headers
        if auth is not None:
            options["auth"] = auth
        return httpx.AsyncClient(**options)

    return factory


__all__ = [
    "PinnedResolverTransport",
    "RefusalSink",
    "pinned_client_factory",
    "proxy_from_environment",
]
