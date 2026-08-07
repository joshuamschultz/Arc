"""Confluence Cloud REST adapter — the whole third-party side of the confluence bundle.

Seeded from the ~/.claude/skills/atlassian-confluence know-how (D-562), which
documents the ``/wiki/rest/api/content`` surface: storage-format bodies, the
version number that must be incremented on every update, and CQL for search.

Credential handling and the two transport rules are the same as the jira
bundle's, for the same reasons: the mechanism does not yet deliver a declared
secret to an attachment, every request carries an explicit timeout, and an id or
a CQL string is data that httpx encodes rather than text spliced into a path.

The update verb reads the current version before it writes. Confluence rejects a
write whose version is not exactly one higher, so guessing would fail on every
page that anyone else has touched.
"""

from __future__ import annotations

import json
import os
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

#: These hold environment variable NAMES, not credentials; the noqa is for
#: the linter's name-based heuristic, which cannot tell the two apart.
ENV_BASE_URL: Final = "ARC_CONFLUENCE_BASE_URL"
ENV_EMAIL: Final = "ARC_CONFLUENCE_EMAIL"
ENV_API_TOKEN: Final = "ARC_CONFLUENCE_API_TOKEN"  # noqa: S105

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
                    f"confluence is not configured: {', '.join(missing)} unset. Arc stores the "
                    f"declared secrets but does not yet hand them to an attachment, so set "
                    f"{ENV_BASE_URL}, {ENV_EMAIL} and {ENV_API_TOKEN} in the agent's environment."
                ),
            )
        try:
            body = await self._get(f"{_API}/space", {"limit": "1"})
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


def _setting(context: dict[str, Any], key: str, env: str) -> str:
    """A credential from the caller's context, else the environment, else empty."""
    value = context.get(key)
    return str(value) if value else os.environ.get(env, "")


def build_native_attachment(context: dict[str, Any]) -> ConfluenceAttachment:
    """The fixed factory Arc calls to build this extension's attachment."""
    return ConfluenceAttachment(
        base_url=_setting(context, "base_url", ENV_BASE_URL),
        email=_setting(context, "email", ENV_EMAIL),
        api_token=_setting(context, "api_token", ENV_API_TOKEN),
    )
