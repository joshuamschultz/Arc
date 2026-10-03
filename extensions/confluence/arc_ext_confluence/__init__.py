"""Confluence Cloud REST adapter — the whole third-party side of the confluence bundle.

Seeded from the ~/.claude/skills/atlassian-confluence know-how (D-562), which
documents the ``/wiki/rest/api/content`` surface: storage-format bodies, the
version number that must be incremented on every update, and CQL for search.

Credential handling: Arc owns the OAuth side. Every request asks the credential
handle for a fresh bearer at the header site and goes to
``api.atlassian.com/ex/confluence/{cloud_id}``; a 401 invalidates the handle and
retries once. Every request carries an explicit timeout, and an id or a CQL string
is data that httpx encodes rather than text spliced into a path.

The update verb reads the current version before it writes. Confluence rejects a
write whose version is not exactly one higher, so guessing would fail on every
page that anyone else has touched.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx
from arcagent.core.errors import ExtensionError
from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceObject,
    SourceObjectKind,
    SourceResource,
    SyncSource,
    SyncSourcePage,
)

if TYPE_CHECKING:
    from arcagent.extension.credential_broker import AccessTokenHandle

#: One flat 30 seconds covered both reaching Confluence and reading a page from
#: it, so a slow moment on either half failed the whole sync — a space that had
#: just indexed cleanly came back as ConnectTimeout on the next run. Connecting
#: keeps a short leash so an unreachable host is reported at once; a transfer
#: already in progress is given room. The per-sync max_seconds ceiling still
#: bounds the run as a whole.
_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0)

#: Atlassian's gateway: one site's Confluence is ``{_GATEWAY}/{cloud_id}``.
_GATEWAY: Final = "https://api.atlassian.com/ex/confluence"

#: The REST root every path below hangs off.
_API: Final = "/wiki/rest/api"

_STRING: Final[dict[str, str]] = {"type": "string"}


class ConfluenceAttachment:
    """Reaches Confluence Cloud over its REST API, through the four hook methods."""

    def __init__(
        self,
        *,
        site: str,
        cloud_id: str,
        credential: AccessTokenHandle,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._site = site.strip()
        self._cloud_id = cloud_id.strip()
        self._base_url = f"{_GATEWAY}/{quote(self._cloud_id, safe='')}" if self._cloud_id else ""
        self._credential = credential
        self._transport = transport
        self._selected_spaces: tuple[str, ...] = ()

    # --- the hook contract ---------------------------------------------------

    def requirements(self) -> list[Requirement]:
        """No sensitive field and no host prerequisite: Arc holds the credential."""
        return []

    async def probe(self) -> ProbeResult:
        """List one space. It is the cheapest call that proves auth and reach."""
        missing = self._missing()
        if missing:
            return ProbeResult(
                reachable=False,
                detail="confluence has no site yet: click Connect on its card to sign in.",
            )
        try:
            body = await self._get(f"{_API}/space", {"limit": "1"})
        except ExtensionError as exc:
            return ProbeResult(
                reachable=False,
                detail=(
                    f"confluence has no usable credential ({exc.code}): "
                    "click Connect on its card to sign in."
                ),
            )
        except httpx.HTTPStatusError as exc:
            return ProbeResult(
                reachable=False, detail=_refused(exc.response.status_code, self._site)
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ProbeResult(reachable=False, detail=f"{self._site} did not answer: {exc}")
        results = body.get("results")
        seen = len(results) if isinstance(results, list) else 0
        return ProbeResult(
            reachable=True,
            tools=await self.describe_tools(),
            detail=f"reached Confluence on {self._site} ({seen} space visible on the first page)",
        )

    async def describe_tools(self) -> list[ToolSpec]:
        """Five verbs. Classification and tags match extension.toml exactly."""
        return [
            ToolSpec(
                name="confluence_search_pages",
                description="Search pages with a CQL query.",
                input_schema=_schema({"cql": _STRING, "limit": _STRING}, required=["cql"]),
                classification="read_only",
            ),
            ToolSpec(
                name="confluence_get_page",
                description="Read one page's title and storage-format body by id.",
                input_schema=_schema({"page_id": _STRING}, required=["page_id"]),
                classification="read_only",
            ),
            ToolSpec(
                name="confluence_list_spaces",
                description="List the spaces this account can see, with their keys.",
                input_schema=_schema({"limit": _STRING}),
                classification="read_only",
            ),
            ToolSpec(
                name="confluence_create_page",
                description="Create a page in a space. Everyone with space access will see it.",
                input_schema=_schema(
                    {
                        "space_key": _STRING,
                        "title": _STRING,
                        "body": _STRING,
                        "parent_id": _STRING,
                    },
                    required=["space_key", "title", "body"],
                ),
                classification="state_modifying",
                capability_tags=["network_egress"],
            ),
            ToolSpec(
                name="confluence_update_page",
                description="Replace a page's title and body. The previous content is superseded.",
                input_schema=_schema(
                    {"page_id": _STRING, "title": _STRING, "body": _STRING},
                    required=["page_id", "title", "body"],
                ),
                classification="state_modifying",
                capability_tags=["network_egress"],
            ),
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Run one verb. A Confluence refusal is an answer, not a raise."""
        try:
            return ToolResult(tool=tool, content=await self._dispatch(tool, args))
        except KeyError:
            return _error(tool, f"confluence has no tool named {tool!r}")
        except ExtensionError as exc:
            return _error(tool, f"confluence has no usable credential ({exc.code})")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            return _error(tool, f"confluence answered {status}: {exc.response.text}")
        except (httpx.HTTPError, ValueError) as exc:
            return _error(tool, f"confluence call failed: {exc}")

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        """Describe this account as a document source without exposing credentials."""
        await self._get(f"{_API}/space", {"limit": "1"})
        return SourceDescription(
            connection_id=request.connection_id,
            display_name=f"Confluence ({self._site})",
            source_kind="confluence",
            account_id=self._site,
            data_shape=SourceDataShape.DOCUMENT,
            supports_incremental=False,
            supports_deletes=False,
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        del request
        results = await self._list_spaces_all()
        return tuple(
            SourceResource(
                resource_id=str(space.get("key", "")),
                label=str(space.get("name") or space.get("key") or "Space"),
                resource_kind="space",
                locator=str(space.get("key", "")),
            )
            for space in results
            if isinstance(space, dict) and space.get("key")
        )

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        resources = await self.list_source_resources(
            ListSourceResources(connection_id=request.connection_id)
        )
        available = {item.resource_id for item in resources}
        if not request.resource_ids or not set(request.resource_ids).issubset(available):
            raise ValueError("invalid Confluence space selection")
        self._selected_spaces = request.resource_ids

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        start = int(request.checkpoint or "0")
        cql = "type=page"
        if self._selected_spaces:
            quoted = ",".join(f'"{key}"' for key in self._selected_spaces)
            cql += f" and space in ({quoted})"
        body = await self._get(
            f"{_API}/content/search",
            {
                "cql": cql,
                "limit": str(request.page_size),
                "start": str(start),
                "expand": "version,space",
            },
        )
        results = body.get("results", [])
        objects = tuple(_source_page(page) for page in results if isinstance(page, dict))
        next_start = start + len(objects)
        links = body.get("_links", {})
        has_more = isinstance(links, dict) and bool(links.get("next"))
        total = body.get("totalSize")
        if isinstance(total, int):
            has_more = next_start < total
        elif len(objects) >= request.page_size and not has_more:
            raise ValueError("Confluence page omitted pagination metadata")
        return SyncSourcePage(
            objects=objects,
            next_checkpoint=str(next_start) if has_more else "0",
            has_more=has_more,
        )

    async def _list_spaces_all(self) -> list[dict[str, Any]]:
        """Walk every visible space, refusing a provider page cap as completion."""
        start = 0
        spaces: list[dict[str, Any]] = []
        while start <= 100_000:
            body = await self._get(f"{_API}/space", {"limit": "200", "start": str(start)})
            page = [item for item in body.get("results", []) if isinstance(item, dict)]
            spaces.extend(page)
            links = body.get("_links", {})
            total = body.get("totalSize")
            has_more = bool(isinstance(links, dict) and links.get("next"))
            if isinstance(total, int):
                has_more = start + len(page) < total
            elif len(page) >= 200 and not has_more:
                raise ValueError("Confluence space page omitted pagination metadata")
            if not has_more:
                return spaces
            if not page:
                raise ValueError("Confluence returned an empty page with more results")
            start += len(page)
        raise ValueError("Confluence space collection exceeds the safe synchronization bound")

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        page = await self._get(
            f"{_API}/content/{_segment(request.object_id)}",
            {"expand": "body.storage,version,space"},
        )
        body = page.get("body", {})
        storage = body.get("storage", {}) if isinstance(body, dict) else {}
        content = str(storage.get("value", "")).encode()
        fetched_version = page.get("version", {})
        number = fetched_version.get("number") if isinstance(fetched_version, dict) else None
        if str(number or "1") != request.version:
            raise ValueError("Confluence page changed during fetch")
        if len(content) > request.max_bytes:
            raise ValueError("Confluence page exceeds byte limit")
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            content=content,
            media_type="text/html",
            metadata={},
        )

    async def close_source(self) -> None:
        """No-op: this adapter creates a bounded client per request."""

    # --- verbs ----------------------------------------------------------------

    async def _dispatch(self, tool: str, args: dict[str, Any]) -> str:
        """Route one verb to its request. ``KeyError`` means an undeclared name."""
        if tool == "confluence_search_pages":
            params = {"cql": str(args["cql"]), "limit": str(args.get("limit", "25"))}
            return _dump(await self._get(f"{_API}/content/search", params))
        if tool == "confluence_get_page":
            return _dump(
                await self._get(
                    f"{_API}/content/{_segment(args['page_id'])}",
                    {"expand": "body.storage,version,space"},
                )
            )
        if tool == "confluence_list_spaces":
            return _dump(await self._get(f"{_API}/space", {"limit": str(args.get("limit", "25"))}))
        if tool == "confluence_create_page":
            return _dump(await self._post(f"{_API}/content", _create_body(args)))
        if tool == "confluence_update_page":
            return await self._update(args)
        raise KeyError(tool)

    async def _update(self, args: dict[str, Any]) -> str:
        """Read the current version, then write version + 1 — Confluence rejects any other."""
        page_id = _segment(args["page_id"])
        current = await self._get(f"{_API}/content/{page_id}", {})
        version = current.get("version")
        number = version.get("number") if isinstance(version, dict) else None
        if not isinstance(number, int):
            msg = f"page {page_id} returned no version number to increment"
            raise ValueError(msg)
        payload = {
            "id": page_id,
            "type": "page",
            "title": str(args["title"]),
            "version": {"number": number + 1},
            "body": _storage(str(args["body"])),
        }
        return _dump(await self._put(f"{_API}/content/{page_id}", payload))

    # --- transport -------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        return _body(await self._send("GET", path, params=params))

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _body(await self._send("POST", path, json=payload))

    async def _put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _body(await self._send("PUT", path, json=payload))

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One request with a fresh bearer; a 401 invalidates the handle and retries once."""
        response = await self._attempt(method, path, **kwargs)
        if response.status_code == 401:
            await self._credential.invalidate()
            response = await self._attempt(method, path, **kwargs)
        return response

    async def _attempt(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """The bearer is fetched now, so a rotation reaches this request."""
        token = (await self._credential.bearer()).reveal()
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
            transport=self._transport,
        ) as client:
            return await client.request(method, path, **kwargs)

    def _missing(self) -> list[str]:
        """Which setting Connect writes this attachment does not have yet."""
        return [] if self._cloud_id else ["cloud_id"]


def _source_page(page: dict[str, Any]) -> SourceObject:
    version = page.get("version", {})
    number = version.get("number") if isinstance(version, dict) else None
    page_id = str(page.get("id") or "")
    return SourceObject(
        object_id=page_id,
        locator=str(page.get("_links", {}).get("webui") or page_id),
        kind=SourceObjectKind.FILE,
        version=str(number or page.get("version") or "1"),
        modified_at=str(version.get("when") or "") if isinstance(version, dict) else None,
        media_type="text/html",
        metadata={
            **page,
            "classification": "unclassified",
            "revision": int(number) if isinstance(number, int) else 1,
        },
    )


def _refused(status: int, site: str) -> str:
    """What the operator should do about the status Atlassian answered the probe with.

    Deliberately this bundle's own copy: this folder imports nothing from Arc but
    the hook's value types, which is the property that makes the bundle deletable.

    401 means the sign-in is no longer accepted (revoked, or the app was removed),
    so the card's Connect is the fix. 403 means the sign-in worked and the account
    or the app's granted scopes do not cover this API. 404 means the stored site
    is not a Confluence site.
    """
    if status == 401:
        return (
            "Atlassian no longer accepts this sign-in. Click Connect on the card to sign in again."
        )
    if status == 403:
        return (
            f"Atlassian accepted the sign-in, but this account or the app's granted scopes "
            f"do not allow the Confluence API on {site}. Ask a site administrator, or "
            "connect again and leave every permission ticked."
        )
    if status == 404:
        return f"{site} answered, but there is no Confluence there. Check the connection's site."
    return f"Atlassian answered {status} for {site}, so the connection could not be checked."


def _body(response: httpx.Response) -> dict[str, Any]:
    """The response as a JSON object, raising for status and for a non-object body."""
    response.raise_for_status()
    if not response.content:
        return {"status": response.status_code}
    parsed = response.json()
    if not isinstance(parsed, dict):
        return {"result": parsed}
    return parsed


def _segment(value: object) -> str:
    """One path segment, refusing anything that could climb or split the path."""
    text = str(value)
    if not text or "/" in text or ".." in text:
        msg = f"invalid Confluence identifier {text!r}"
        raise ValueError(msg)
    return text


def _create_body(args: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "page",
        "title": str(args["title"]),
        "space": {"key": str(args["space_key"])},
        "body": _storage(str(args["body"])),
    }
    parent_id = str(args.get("parent_id", ""))
    if parent_id:
        payload["ancestors"] = [{"id": _segment(parent_id)}]
    return payload


def _storage(value: str) -> dict[str, Any]:
    """A body in Confluence storage format — XHTML, which is what the API accepts."""
    return {"storage": {"value": value, "representation": "storage"}}


def _schema(
    properties: dict[str, dict[str, str]], *, required: list[str] | None = None
) -> dict[str, Any]:
    """One tool's input schema, closed to anything the verb did not declare."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _dump(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False)


def _error(tool: str, content: str) -> ToolResult:
    return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=content)


def build_native_attachment(context: dict[str, Any]) -> ConfluenceAttachment:
    """The fixed factory Arc calls to build this extension's attachment."""
    return ConfluenceAttachment(
        site=str(context.get("site") or ""),
        cloud_id=str(context.get("cloud_id") or ""),
        credential=context["credential"],
        transport=context.get("transport"),
    )
