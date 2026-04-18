"""Network egress proxy for dynamic tools — SPEC-017 R-055.

Deny-by-default outbound HTTP. Dynamic tools reach the network only
through :class:`EgressProxy`. Every call is matched against a
per-tool allowlist of origins (scheme + host + port) before the
request is dispatched. Both allowed and denied requests emit audit
events so operators can reconstruct data flow.

This module has no hard dependency on ``httpx`` — callers inject a
``send_fn`` so we can substitute a fake in tests and a real HTTP
client in production.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from arcagent.core.errors import ArcAgentError

_logger = logging.getLogger("arcagent.tools.egress")

AuditSink = Callable[[str, dict[str, Any]], None]
SendFn = Callable[..., Awaitable[Any]]


@runtime_checkable
class ResponseLike(Protocol):
    """Minimal shape required from the HTTP response."""

    status_code: int


class EgressDenied(ArcAgentError):  # noqa: N818 — domain convention: peers are *Error/ArcAgentError
    """Raised when a dynamic tool attempts to reach a non-allowlisted origin."""

    _component = "tools_egress"

    def __init__(self, url: str, origin: str, allowlist: set[str]) -> None:
        super().__init__(
            code="EGRESS_DENIED",
            message=f"Egress to {origin!r} blocked (url={url}); allowlist={sorted(allowlist)}",
            details={"url": url, "origin": origin, "allowlist": sorted(allowlist)},
        )


class EgressProxy:
    """Per-tool HTTP egress gate.

    Parameters
    ----------
    allowlist:
        Set of origin strings (``scheme://host[:port]``). Empty =
        deny everything.
    send_fn:
        Async callable ``send_fn(url, method, **kwargs) -> response``.
        Injected for testability; production callers pass an
        ``httpx.AsyncClient``-shaped coroutine.
    audit_sink:
        Optional ``(event, payload)`` callback fired on every request.
    """

    def __init__(
        self,
        *,
        allowlist: set[str],
        send_fn: SendFn,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._allowlist = {_normalize(o) for o in allowlist}
        self._send_fn = send_fn
        self._audit_sink = audit_sink

    async def request(
        self,
        url: str,
        *,
        method: str = "GET",
        **kwargs: Any,
    ) -> Any:
        """Dispatch an HTTP request through the proxy.

        Raises :class:`EgressDenied` when the target origin is not in
        the per-tool allowlist. Otherwise forwards to ``send_fn`` and
        returns its response.
        """
        origin = _origin_of(url)
        if origin not in self._allowlist:
            self._emit("egress.denied", {"url": url, "origin": origin, "method": method})
            raise EgressDenied(url=url, origin=origin, allowlist=self._allowlist)

        try:
            response = await self._send_fn(url, method, **kwargs)
        except Exception as exc:
            self._emit(
                "egress.error",
                {"url": url, "origin": origin, "method": method, "error": str(exc)},
            )
            raise

        self._emit(
            "egress.allowed",
            {
                "url": url,
                "origin": origin,
                "method": method,
                "status_code": getattr(response, "status_code", 0),
            },
        )
        return response

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(event, payload)
        except Exception:
            _logger.exception("Egress audit sink raised; continuing")


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    if parts.port is not None:
        return f"{parts.scheme}://{parts.hostname}:{parts.port}"
    return f"{parts.scheme}://{parts.hostname}"


def _normalize(origin: str) -> str:
    """Normalize allowlist entries so callers can pass full URLs too."""
    parts = urlsplit(origin)
    if parts.scheme and parts.hostname:
        return _origin_of(origin)
    return origin


__all__ = ["EgressDenied", "EgressProxy"]
