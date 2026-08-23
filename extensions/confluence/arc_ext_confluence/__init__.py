"""Confluence Cloud REST adapter — the whole third-party side of the confluence bundle.

Seeded from the ~/.claude/skills/atlassian-confluence know-how (D-562), which
documents the ``/wiki/rest/api/content`` surface: storage-format bodies, the
version number that must be incremented on every update, and CQL for search.

Credential handling and the two transport rules are the same as the jira
bundle's, for the same reasons: the declared secrets arrive in the factory's
context, resolved from Arc's secret store for this connected instance and from
nowhere else, every request carries an explicit timeout, and an id or a CQL
string is data that httpx encodes rather than text spliced into a path.

The update verb reads the current version before it writes. Confluence rejects a
write whose version is not exactly one higher, so guessing would fail on every
page that anyone else has touched.
"""

from __future__ import annotations

import json
from typing import Any, Final

import httpx
from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    RequirementKind,
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

#: Seconds any one Confluence request may take before it is abandoned.
_TIMEOUT: Final = 30.0

#: The REST root every path below hangs off.
_API: Final = "/wiki/rest/api"

_STRING: Final[dict[str, str]] = {"type": "string"}


class ConfluenceAttachment:
    """Reaches Confluence Cloud over its REST API, through the four hook methods."""

    def __init__(self, *, base_url: str, email: str, api_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._email = email
        self._api_token = api_token
        self._selected_spaces: tuple[str, ...] = ()

    # --- the hook contract ---------------------------------------------------

    def requirements(self) -> list[Requirement]:
        """Three credentials and no host prerequisite: the transport is httpx."""
        return [
            Requirement(kind=RequirementKind.CREDENTIAL, name=name, instruction=instruction)
            for name, instruction in (
                ("base_url", "Confluence base URL, e.g. https://yourcompany.atlassian.net"),
                ("email", "The Atlassian account email the API token belongs to"),
                ("api_token", "An Atlassian API token from id.atlassian.com"),
            )
        ]

    async def probe(self) -> ProbeResult:
        """List one space. It is the cheapest call that proves auth and reach."""
        missing = self._missing()
        if missing:
            return ProbeResult(
                reachable=False,
                detail=(
                    f"confluence has no credential for {', '.join(missing)} — "
                    f"run 'arc connector auth <instance>' to supply them."
                ),
            )
        try:
            body = await self._get(f"{_API}/space", {"limit": "1"})
        except httpx.HTTPStatusError as exc:
            return ProbeResult(
                reachable=False, detail=_refused(exc.response.status_code, self._base_url)
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ProbeResult(reachable=False, detail=f"{self._base_url} did not answer: {exc}")
        results = body.get("results")
        seen = len(results) if isinstance(results, list) else 0
        return ProbeResult(
            reachable=True,
            tools=await self.describe_tools(),
            detail=f"reached {self._base_url}/wiki ({seen} space visible on the first page)",
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
            display_name=f"Confluence ({self._base_url})",
            source_kind="confluence",
            account_id=self._base_url,
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
            body = await self._get(
                f"{_API}/space", {"limit": "200", "start": str(start)}
            )
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
        async with self._client() as client:
            response = await client.get(path, params=params)
        return _body(response)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(path, json=payload)
        return _body(response)

    async def _put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.put(path, json=payload)
        return _body(response)

    def _client(self) -> httpx.AsyncClient:
        """One client per call. There is no close() on the hook to release a shared one."""
        return httpx.AsyncClient(
            base_url=self._base_url,
            auth=(self._email, self._api_token),
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )

    def _missing(self) -> list[str]:
        """Which of the three credentials this attachment does not have."""
        held = {"base_url": self._base_url, "email": self._email, "api_token": self._api_token}
        return sorted(name for name, value in held.items() if not value)


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


def _refused(status: int, base_url: str) -> str:
    """What the operator should do about the status Atlassian answered the probe with.

    The same three verdicts as the jira bundle's, and deliberately its own copy:
    this folder imports nothing from Arc but the hook's value types, which is the
    property that makes the bundle deletable. Sharing a helper between two bundles
    would put a third thing in the middle that neither of them owns.

    401 means the email and token are not one account (or the token is revoked) —
    a perfectly valid token can still be the wrong one, so both fields are named.
    403 means the sign-in worked and the account may not use this API. 404 means
    the address is not a Confluence site, which is a third field entirely.

    The 401 also names the THIRD thing it can now mean, because that one cost an
    operator an afternoon on the sibling connector: Atlassian has begun issuing
    SCOPED API tokens, and a scoped token is refused at ``{site}.atlassian.net``
    however correct it is. It only works against
    ``api.atlassian.com/ex/confluence/{cloudId}``, which this adapter does not yet
    speak. Sending someone to reissue a perfectly good token is the worst possible
    instruction, so the message says which kind of token this address accepts.
    """
    if status == 401:
        return (
            "Atlassian refused the email and the API token together. Three things do "
            "this. The email may not be the one you sign in to Atlassian with — they "
            "have to be the same account. The token may have been revoked, in which "
            "case create a new one at id.atlassian.com/manage-profile/security/"
            "api-tokens. Or it may be one of Atlassian's newer SCOPED tokens: this "
            "connection can only use an UNSCOPED one, because a scoped token is "
            "accepted only at api.atlassian.com and not at your site address. When "
            "you create the token, do not add scopes to it."
        )
    if status == 403:
        return (
            f"Atlassian accepted the sign-in, but this account is not permitted to use "
            f"the Confluence API on {base_url}. Ask a site administrator to give it access."
        )
    if status == 404:
        return (
            f"{base_url} answered, but there is no Confluence there. Check the address is "
            f"the one your browser bar shows when you are looking at a page; it usually "
            f"ends in .atlassian.net."
        )
    return f"Atlassian answered {status} for {base_url}, so the connection could not be checked."


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


def _credential(context: dict[str, Any], key: str) -> str:
    """One declared credential out of the context Arc resolved from its secret store."""
    return str(context.get(key) or "")


def build_native_attachment(context: dict[str, Any]) -> ConfluenceAttachment:
    """The fixed factory Arc calls to build this extension's attachment."""
    return ConfluenceAttachment(
        base_url=_credential(context, "base_url"),
        email=_credential(context, "email"),
        api_token=_credential(context, "api_token"),
    )
