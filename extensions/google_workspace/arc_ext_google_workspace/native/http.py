"""The one HTTP helper every Google tool shares, plus the argument readers they use.

Arc owns the OAuth side: the credential handle serves a fresh bearer at the header
site of every request, and a 401 makes the helper tell the handle to invalidate
and try once more. Rate limits and server errors are retried up to three times,
honouring ``Retry-After`` up to ten seconds.

Error text is built for two readers: the agent, and the connector health
classifier. It carries the HTTP status and Google's own reason, never the bearer
token and never a request URL (a Gmail query can sit in the URL).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final
from urllib.parse import quote

import httpx
from arcagent.extension.source import api_disabled_link, api_disabled_name

_TIMEOUT: Final = httpx.Timeout(30, connect=10)
_LIMITS: Final = httpx.Limits(max_connections=10)
_MAX_ATTEMPTS: Final = 3
_MAX_RETRY_AFTER_SECONDS: Final = 10.0
_BACKOFF_SECONDS: Final = 0.5
_MESSAGE_CAP: Final = 200
#: How much of Google's message is read, so the console link inside a long
#: "API not enabled" sentence is found before the text is cut for display.
_MESSAGE_READ_CAP: Final = 2000
_REASON_API_DISABLED: Final = "accessNotConfigured"

#: What Google's error body omits, by status, so the text always names a reason.
_DEFAULT_REASON: Final = {
    401: "unauthenticated",
    403: "forbidden",
    404: "notFound",
    429: "rateLimitExceeded",
}

Sleep = Callable[[float], Awaitable[None]]
Params = Mapping[str, str | int | list[str]]


class ToolError(Exception):
    """A tool call that failed with text safe to show the agent."""


class GoogleApiError(ToolError):
    """Google answered with an error status. ``status`` is the HTTP code."""

    def __init__(self, status: int, text: str) -> None:
        super().__init__(text)
        self.status = status


def encode_id(value: str) -> str:
    """One URL path segment: an id is data, never a path."""
    return quote(value, safe="")


class GoogleHttp:
    """Authenticated JSON requests against the Google REST APIs."""

    def __init__(
        self,
        credential: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self._credential = credential
        self._transport = transport
        self._sleep = sleep
        self._client = self._new_client()

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_TIMEOUT, limits=_LIMITS, transport=self._transport)

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client.is_closed:
            self._client = self._new_client()
        return self._client

    async def aclose(self) -> None:
        """Release the connection pool."""
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Params | None = None,
        body: Any = None,
    ) -> Any:
        """Send one request and return the parsed JSON (``{}`` for an empty body)."""
        response = await self._send(method, url, params, body)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ToolError("Google returned a body that is not JSON") from exc

    async def request_bytes(self, method: str, url: str, *, params: Params | None = None) -> bytes:
        """Send one request and return the raw body (a file's content, not JSON)."""
        return (await self._send(method, url, params, None)).content

    async def _send(
        self, method: str, url: str, params: Params | None, body: Any
    ) -> httpx.Response:
        refreshed = False
        attempt = 0
        while True:
            response = await self._attempt(method, url, params, body)
            if response.status_code == 401 and not refreshed:
                refreshed = True
                await self._credential.invalidate()
                continue
            attempt += 1
            if _retryable(response.status_code) and attempt < _MAX_ATTEMPTS:
                await (self._sleep or _pause)(_retry_delay(response, attempt))
                continue
            if response.is_error:
                raise _api_error(response)
            return response

    async def _attempt(
        self, method: str, url: str, params: Params | None, body: Any
    ) -> httpx.Response:
        token = (await self._credential.bearer()).reveal()
        try:
            return await self._http.request(
                method,
                url,
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"Google did not answer ({type(exc).__name__})") from exc


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After", "")
    try:
        return min(max(float(header), 0.0), _MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return _BACKOFF_SECONDS * attempt


def _api_error(response: httpx.Response) -> GoogleApiError:
    status = response.status_code
    reason, google_status, message = _error_fields(response)
    reason = reason or google_status or _DEFAULT_REASON.get(status, "error")
    parts = [str(status), reason]
    if google_status and google_status.casefold() != reason.casefold():
        parts.append(google_status)
    text = " ".join(parts)
    if reason == _REASON_API_DISABLED:
        return GoogleApiError(status, _api_disabled_text(text, message))
    message = message[:_MESSAGE_CAP]
    return GoogleApiError(status, f"Google API error {text}: {message}" if message else text)


def _api_disabled_text(text: str, message: str) -> str:
    """One short sentence that keeps the console link whole, or no link at all.

    Google's own sentence is long enough that the display cap cut the link off, and
    a link from any host but Google's console is dropped here, so the text an agent
    reads never carries a URL a hostile error body chose.
    """
    name = api_disabled_name(message)
    link = api_disabled_link(message)
    where = f"; enable it at {link}" if link else ""
    return f"Google API error {text}: {name} is not enabled for this project{where}"


def _error_fields(response: httpx.Response) -> tuple[str, str, str]:
    try:
        payload = response.json()
    except ValueError:
        return "", "", ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, str):
        # OAuth-style errors: {"error": "invalid_grant", "error_description": ...}.
        return error, "", ""
    if not isinstance(error, dict):
        return "", "", ""
    details = error.get("errors")
    reason = ""
    if isinstance(details, list) and details and isinstance(details[0], dict):
        reason = str(details[0].get("reason") or "")
    message = str(error.get("message") or "")[:_MESSAGE_READ_CAP]
    return reason, str(error.get("status") or ""), message


# --- argument readers ---------------------------------------------------------


def text_arg(args: Mapping[str, Any], name: str, *, required: bool = False) -> str:
    """A string argument, stripped. A missing required one is a tool error."""
    value = args.get(name)
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise ToolError(f"{name} is required")
    return text


def int_arg(args: Mapping[str, Any], name: str, *, default: int, ceiling: int) -> int:
    """An integer argument clamped to ``1..ceiling``; the source path passes strings."""
    raw = args.get(name)
    if raw is None or raw == "":
        return min(default, ceiling)
    try:
        number = int(raw)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{name} must be a whole number") from exc
    return max(1, min(number, ceiling))


def list_arg(args: Mapping[str, Any], name: str) -> list[str]:
    """A comma-separated argument as a list of non-empty items."""
    return [item.strip() for item in text_arg(args, name).split(",") if item.strip()]


def drop_empty(params: Mapping[str, Any]) -> dict[str, Any]:
    """Query parameters without the unset ones."""
    return {key: value for key, value in params.items() if value not in ("", None, [])}
