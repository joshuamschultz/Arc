"""The one Microsoft Graph client every Microsoft 365 tool and source shares.

Arc owns the OAuth side: the credential handle serves a fresh bearer at the header
site of every request, and a 401 makes the client tell the handle to invalidate and
try once more. 429 / 503 / 504 are retried up to three times, honouring
``Retry-After`` up to ten seconds.

**Egress is pinned to one host.** A client is built for ONE cloud's Graph host (from
the signed manifest's cloud table, chosen by key). Every URL — a relative path, or
an ``@odata.nextLink`` / ``@odata.deltaLink`` Graph handed back — must be ``https``
on exactly that host under ``/v1.0/``, and is checked BEFORE a bearer is attached.
A hostile or corrupted paging link therefore never receives the token.

**Graph's words never reach the model.** An error becomes ``Microsoft Graph error
<status> <code>``, where ``<code>`` is Graph's ``error.code`` only when it is a plain
identifier; Graph's ``message`` (which can carry request details or text an
attacker planted in a resource name) is dropped.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final
from urllib.parse import quote, urlsplit

import httpx

#: Cloud KEY -> Microsoft Graph host. Mirrors ``[oauth.clouds]`` in extension.toml
#: (a test holds the two equal); the key is the only thing an app slot stores.
GRAPH_HOSTS: Final[Mapping[str, str]] = {
    "global": "graph.microsoft.com",
    "usgov": "graph.microsoft.us",
    "dod": "dod-graph.microsoft.us",
}
DEFAULT_CLOUD: Final = "global"
API_VERSION: Final = "/v1.0"

_TIMEOUT: Final = httpx.Timeout(30, connect=10)
_LIMITS: Final = httpx.Limits(max_connections=10)
_MAX_ATTEMPTS: Final = 3
_MAX_RETRY_AFTER_SECONDS: Final = 10.0
_BACKOFF_SECONDS: Final = 0.5
_RETRYABLE: Final = frozenset({429, 503, 504})
_PLAIN_CODE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}$")
_DEFAULT_CODE: Final = {
    400: "badRequest",
    401: "unauthenticated",
    403: "forbidden",
    404: "itemNotFound",
    429: "tooManyRequests",
}

Sleep = Callable[[float], Awaitable[None]]
Params = Mapping[str, str | int]


class ToolError(Exception):
    """A failure with text safe to show the agent."""


class GraphError(ToolError):
    """Graph answered with an error status. Carries the status and ``Retry-After`` only."""

    def __init__(self, status: int, code: str, *, retry_after: str | None = None) -> None:
        super().__init__(f"Microsoft Graph error {status} {code}")
        self.status_code = status
        self.code = code
        self.headers: dict[str, str] = {} if retry_after is None else {"Retry-After": retry_after}


class EgressRefusedError(ToolError):
    """A URL that is not on this connection's pinned Graph host. No request was sent."""


def graph_host(cloud: str) -> str:
    """The Graph host for a cloud KEY; "" for a key Microsoft 365 does not have."""
    return GRAPH_HOSTS.get(cloud or DEFAULT_CLOUD, "")


def segment(value: str) -> str:
    """One URL path segment: an id is data, never a path."""
    return quote(value, safe="")


class GraphClient:
    """Authenticated requests against one cloud's Microsoft Graph."""

    def __init__(
        self,
        credential: Any,
        *,
        cloud: str = DEFAULT_CLOUD,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self._credential = credential
        #: "" for an unknown cloud key: every request is then refused, never guessed.
        self.host = graph_host(cloud)
        self.base = f"https://{self.host}{API_VERSION}"
        self._transport = transport
        self._sleep = sleep
        self._client = self._new_client()

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=_TIMEOUT, limits=_LIMITS, transport=self._transport, follow_redirects=False
        )

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client.is_closed:
            self._client = self._new_client()
        return self._client

    async def aclose(self) -> None:
        """Release the connection pool."""
        await self._client.aclose()

    def url(self, path_or_link: str) -> str:
        """The absolute URL for a relative Graph path, or a checked Graph-issued link.

        Raises:
            EgressRefusedError: anything not ``https://<pinned host>/v1.0/...``.
            ToolError: the connection names a cloud Microsoft 365 does not have.
        """
        if not self.host:
            raise ToolError("this connection names a cloud Microsoft 365 does not have")
        if path_or_link.startswith("/"):
            if path_or_link.startswith(API_VERSION + "/"):
                path_or_link = path_or_link[len(API_VERSION) :]
            candidate = self.base + path_or_link
        else:
            candidate = path_or_link
        parts = urlsplit(candidate)
        try:
            port = parts.port
        except ValueError:
            port = -1
        if (
            parts.scheme != "https"
            or (parts.hostname or "").casefold() != self.host
            or port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or not parts.path.startswith(API_VERSION + "/")
            or "/../" in parts.path
        ):
            raise EgressRefusedError("refused a link that is not on this connection's Graph host")
        return candidate

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Params | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send one request (retried, refreshed once on 401) and return it unread.

        The caller owns the returned response and closes it (``aclose``); it is
        streamed so a file read can stop at a byte ceiling.

        Raises:
            EgressRefusedError: the URL is off the pinned host (nothing was sent).
            GraphError: Graph's final answer was an error status.
            ToolError: Graph could not be reached.
        """
        url = self.url(path)
        refreshed = False
        attempt = 0
        while True:
            response = await self._attempt(method, url, params, json, headers)
            if response.status_code == 401 and not refreshed:
                refreshed = True
                await response.aclose()
                await self._credential.invalidate()
                continue
            attempt += 1
            if response.status_code in _RETRYABLE and attempt < _MAX_ATTEMPTS:
                delay = _retry_delay(response, attempt)
                await response.aclose()
                await (self._sleep or asyncio.sleep)(delay)
                continue
            if response.is_error:
                raise await _graph_error(response)
            return response

    async def get_json(self, path: str, *, params: Params | None = None) -> dict[str, Any]:
        """GET one JSON object."""
        return await self.send_json("GET", path, params=params)

    async def send_json(
        self,
        method: str,
        path: str,
        *,
        params: Params | None = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send one request and return its JSON object (``{}`` for an empty body)."""
        response = await self.request(method, path, params=params, json=body, headers=headers)
        try:
            raw = await response.aread()
        finally:
            await response.aclose()
        if not raw:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise ToolError("Microsoft Graph returned a body that is not JSON") from exc
        if not isinstance(payload, dict):
            raise ToolError("Microsoft Graph returned an unexpected body")
        return payload

    async def _attempt(
        self,
        method: str,
        url: str,
        params: Params | None,
        body: Any,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response:
        token = (await self._credential.bearer()).reveal()
        request = self._http.build_request(
            method,
            url,
            params=params,
            json=body,
            headers={**(headers or {}), "Authorization": f"Bearer {token}"},
        )
        try:
            return await self._http.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise ToolError(f"Microsoft Graph did not answer ({type(exc).__name__})") from None


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After", "")
    try:
        return min(max(float(header), 0.0), _MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return _BACKOFF_SECONDS * attempt


async def _graph_error(response: httpx.Response) -> GraphError:
    """The sanitized error: status and Graph's plain error code, nothing else."""
    status = response.status_code
    retry_after = response.headers.get("Retry-After")
    try:
        await response.aread()
        payload = response.json()
    except (ValueError, httpx.HTTPError):
        payload = None
    finally:
        await response.aclose()
    code = ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        code = error["code"]
    elif isinstance(error, str):
        code = error
    if not _PLAIN_CODE.fullmatch(code):
        code = _DEFAULT_CODE.get(status, "error")
    hint = retry_after if retry_after and retry_after.isdigit() else None
    return GraphError(status, code, retry_after=hint)


__all__ = [
    "API_VERSION",
    "DEFAULT_CLOUD",
    "GRAPH_HOSTS",
    "EgressRefusedError",
    "GraphClient",
    "GraphError",
    "ToolError",
    "graph_host",
    "segment",
]
