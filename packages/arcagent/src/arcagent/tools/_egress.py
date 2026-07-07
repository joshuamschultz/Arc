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

from arctrust.classification import dominates, parse_classification

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


class EgressClassificationDenied(ArcAgentError):  # noqa: N818 — domain convention
    """Raised when egress data classification exceeds the destination clearance."""

    _component = "tools_egress"

    def __init__(self, url: str, origin: str, data_classification: str, ceiling: str) -> None:
        super().__init__(
            code="EGRESS_CLASSIFICATION_REFUSED",
            message=(
                f"Egress of {data_classification!r} data to {origin!r} refused "
                f"(destination cleared only to {ceiling!r}); url={url}"
            ),
            details={
                "url": url,
                "origin": origin,
                "data_classification": data_classification,
                "destination_clearance": ceiling,
            },
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
    external_ceiling:
        The single deployment-wide external egress ceiling (SPEC-038 OQ-5):
        nothing classified above this leaves. Default ``UNCLASSIFIED``.
    origin_clearances:
        Optional per-origin clearance overrides (``origin -> label``) for
        allowlisted destinations cleared above the external ceiling.
    """

    def __init__(
        self,
        *,
        allowlist: set[str],
        send_fn: SendFn,
        audit_sink: AuditSink | None = None,
        external_ceiling: str = "UNCLASSIFIED",
        origin_clearances: dict[str, str] | None = None,
        data_classifier: Callable[[], str] | None = None,
        strict: bool = False,
    ) -> None:
        self._allowlist = {_normalize(o) for o in allowlist}
        self._send_fn = send_fn
        self._audit_sink = audit_sink
        self._external_ceiling = external_ceiling
        self._origin_clearances = {
            _normalize(o): label for o, label in (origin_clearances or {}).items()
        }
        # SPEC-038 F2 — resolves the CALLING session's max-read classification so
        # the no-exfil check uses the real data label, not a hardcoded default.
        # When absent (standalone/tests) the data label defaults UNCLASSIFIED.
        self._data_classifier = data_classifier
        # Federal parses labels strict: an unknown data/destination label raises
        # (fail closed) rather than defaulting permissive (REQ-026 / F5).
        self._strict = strict

    async def request(
        self,
        url: str,
        *,
        method: str = "GET",
        data_classification: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Dispatch an HTTP request through the proxy.

        Raises :class:`EgressDenied` when the target origin is not in the
        per-tool allowlist, or :class:`EgressClassificationDenied` when the
        data classification exceeds the destination's clearance (SPEC-038
        REQ-025 no-exfil). Otherwise forwards to ``send_fn``.

        ``data_classification`` defaults to the calling session's max-read
        classification (resolved from the injected ``data_classifier``) so no
        caller can silently exfil above-clearance data by omitting the label.
        """
        origin = self._gate(url, method, data_classification)
        try:
            response = await self._send_fn(url, method, **kwargs)
        except Exception as exc:  # reason: re-raise after log
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

    async def authorize(
        self, url: str, *, method: str = "POST", data_classification: str | None = None
    ) -> None:
        """Mediate an outbound comm whose bytes travel over a vetted client (F3).

        For comms tools whose transport is a dedicated API client (e.g. the
        Telegram bot library) rather than raw ``httpx``, this applies the SAME
        allowlist + no-exfil checks and records the ``external_comms`` leg — the
        single mediation point — then returns so the client can deliver. Raises
        :class:`EgressDenied` / :class:`EgressClassificationDenied` on refusal.
        """
        origin = self._gate(url, method, data_classification)
        self._emit("egress.allowed", {"url": url, "origin": origin, "method": method})

    def _gate(self, url: str, method: str, data_classification: str | None) -> str:
        """Run the allowlist + no-exfil checks; return the origin or raise."""
        label = data_classification if data_classification is not None else self._data_class()
        origin = _origin_of(url)
        if origin not in self._allowlist:
            self._emit("egress.denied", {"url": url, "origin": origin, "method": method})
            raise EgressDenied(url=url, origin=origin, allowlist=self._allowlist)

        dest_label = self._origin_clearances.get(origin, self._external_ceiling)
        if not dominates(
            parse_classification(dest_label, strict=self._strict),
            parse_classification(label, strict=self._strict),
        ):
            self._emit(
                "egress.classification_refused",
                {
                    "url": url,
                    "origin": origin,
                    "data_classification": label,
                    "destination_clearance": dest_label,
                },
            )
            raise EgressClassificationDenied(
                url=url, origin=origin, data_classification=label, ceiling=dest_label
            )
        return origin

    def _data_class(self) -> str:
        """Resolve the calling session's max-read classification (SPEC-038 F2).

        No resolver wired (standalone/tests) → UNCLASSIFIED. When a resolver is
        present it reads the live session ledger; a session that has read nothing
        classified resolves to UNCLASSIFIED, while a SECRET read this session
        raises the label so the no-exfil check bites.
        """
        if self._data_classifier is None:
            return "UNCLASSIFIED"
        return self._data_classifier()

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(event, payload)
        except Exception:  # reason: fail-open — log + continue
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


__all__ = ["EgressClassificationDenied", "EgressDenied", "EgressProxy"]
