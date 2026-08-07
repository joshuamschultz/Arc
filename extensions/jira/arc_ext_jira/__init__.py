"""Jira Cloud REST v3 adapter — the whole third-party side of the jira bundle.

D-562: no third-party Atlassian server is adopted, so this folder owns the
transport. It imports nothing from Arc except the hook's own value types, which
is the property that makes it deletable: remove the bundle and this code, its
knowledge of Jira, and its dependency all go with it.

**Credentials.** The mechanism collects the ``[[secrets]]`` this bundle declares
and writes them to the secret store, but nothing hands them to an attachment —
``build_attachment`` passes only ``{"bundle": ...}``. So the adapter reads the
context first (it will simply start working the day the mechanism threads a
credential or a broker grant through it) and the environment second. When
neither has them, :meth:`JiraAttachment.probe` refuses by name rather than
failing later with a 401 nobody can read.

Two rules shape every request. Every call is bounded by an explicit timeout, so a
hung Atlassian endpoint cannot hold a turn open. And a JQL string or an issue key
is data: it travels as a query parameter or a JSON value that httpx encodes, and
is never concatenated into a path.
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

#: Where each credential is looked for, in the environment.
#: These hold environment variable NAMES, not credentials; the noqa is for
#: the linter's name-based heuristic, which cannot tell the two apart.
ENV_BASE_URL: Final = "ARC_JIRA_BASE_URL"
ENV_EMAIL: Final = "ARC_JIRA_EMAIL"
ENV_API_TOKEN: Final = "ARC_JIRA_API_TOKEN"  # noqa: S105

#: Seconds any one Jira request may take before it is abandoned.
_TIMEOUT: Final = 30.0

#: The fields read verbs return. Fixed here rather than model-chosen: an issue
#: carries far more than an agent needs, and every extra field is context spent.
_ISSUE_FIELDS: Final = (
    "summary,status,assignee,reporter,priority,issuetype,project,created,updated"
)

_STRING: Final[dict[str, str]] = {"type": "string"}


class JiraAttachment:
    """Reaches Jira Cloud over its REST v3 API, through the four hook methods."""

    def __init__(self, *, base_url: str, email: str, api_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._email = email
        self._api_token = api_token

    # --- the hook contract ---------------------------------------------------

    def requirements(self) -> list[Requirement]:
        """Three credentials and no host prerequisite: the transport is httpx."""
        return [
            Requirement(
                kind=RequirementKind.CREDENTIAL,
                name=name,
                instruction=instruction,
            )
            for name, instruction in (
                ("base_url", "Jira base URL, e.g. https://yourcompany.atlassian.net"),
                ("email", "The Atlassian account email the API token belongs to"),
                ("api_token", "A Jira Cloud API token from id.atlassian.com"),
            )
        ]

    async def probe(self) -> ProbeResult:
        """Ask Jira who we are. It is the cheapest call that proves auth works."""
        missing = self._missing()
        if missing:
            return ProbeResult(
                reachable=False,
                detail=(
                    f"jira is not configured: {', '.join(missing)} unset. Arc stores the "
                    f"declared secrets but does not yet hand them to an attachment, so set "
                    f"{ENV_BASE_URL}, {ENV_EMAIL} and {ENV_API_TOKEN} in the agent's "
                    f"environment."
                ),
            )
        try:
            body = await self._get("/rest/api/3/myself", {})
        except (httpx.HTTPError, ValueError) as exc:
            return ProbeResult(reachable=False, detail=f"{self._base_url} did not answer: {exc}")
        who = body.get("displayName") or body.get("emailAddress") or "an account"
        return ProbeResult(
            reachable=True, tools=await self.describe_tools(), detail=f"authenticated as {who}"
        )

    async def describe_tools(self) -> list[ToolSpec]:
        """Six verbs. Classification and tags match extension.toml exactly."""
        return [
            ToolSpec(
                name="jira_search_issues",
                description="Search issues with a JQL query, newest first.",
                input_schema=_schema({"jql": _STRING, "max_results": _STRING}, required=["jql"]),
                classification="read_only",
            ),
            ToolSpec(
                name="jira_get_issue",
                description="Read one issue by its key, e.g. PROJ-123.",
                input_schema=_schema({"issue_key": _STRING}, required=["issue_key"]),
                classification="read_only",
            ),
            ToolSpec(
                name="jira_list_projects",
                description="List the projects this account can see.",
                input_schema=_schema({"query": _STRING}),
                classification="read_only",
            ),
            ToolSpec(
                name="jira_create_issue",
                description="Create an issue. Everyone watching the project will see it.",
                input_schema=_schema(
                    {
                        "project_key": _STRING,
                        "summary": _STRING,
                        "description": _STRING,
                        "issue_type": _STRING,
                    },
                    required=["project_key", "summary"],
                ),
                classification="state_modifying",
                capability_tags=["network_egress"],
            ),
            ToolSpec(
                name="jira_add_comment",
                description="Comment on an issue. Everyone watching it will see the comment.",
                input_schema=_schema(
                    {"issue_key": _STRING, "body": _STRING}, required=["issue_key", "body"]
                ),
                classification="state_modifying",
                capability_tags=["network_egress"],
            ),
            ToolSpec(
                name="jira_transition_issue",
                description="Move an issue to another workflow state by transition id.",
                input_schema=_schema(
                    {"issue_key": _STRING, "transition_id": _STRING},
                    required=["issue_key", "transition_id"],
                ),
                classification="state_modifying",
            ),
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Run one verb. A Jira refusal is an answer the agent reads, not a raise."""
        try:
            return ToolResult(tool=tool, content=await self._dispatch(tool, args))
        except KeyError:
            return _error(tool, f"jira has no tool named {tool!r}")
        except httpx.HTTPStatusError as exc:
            return _error(tool, f"jira answered {exc.response.status_code}: {exc.response.text}")
        except (httpx.HTTPError, ValueError) as exc:
            return _error(tool, f"jira call failed: {exc}")

    # --- verbs ----------------------------------------------------------------

    async def _dispatch(self, tool: str, args: dict[str, Any]) -> str:
        """Route one verb to its request. ``KeyError`` means an undeclared name."""
        if tool == "jira_search_issues":
            return await self._search(args)
        if tool == "jira_get_issue":
            return _dump(
                await self._get(
                    f"/rest/api/3/issue/{_segment(args['issue_key'])}", {"fields": _ISSUE_FIELDS}
                )
            )
        if tool == "jira_list_projects":
            return _dump(await self._get("/rest/api/3/project/search", _projects_params(args)))
        if tool == "jira_create_issue":
            return _dump(await self._post("/rest/api/3/issue", _create_issue_body(args)))
        if tool == "jira_add_comment":
            return _dump(
                await self._post(
                    f"/rest/api/3/issue/{_segment(args['issue_key'])}/comment",
                    {"body": _document(str(args["body"]))},
                )
            )
        if tool == "jira_transition_issue":
            return _dump(
                await self._post(
                    f"/rest/api/3/issue/{_segment(args['issue_key'])}/transitions",
                    {"transition": {"id": str(args["transition_id"])}},
                )
            )
        raise KeyError(tool)

    async def _search(self, args: dict[str, Any]) -> str:
        params = {
            "jql": str(args["jql"]),
            "fields": _ISSUE_FIELDS,
            "maxResults": str(args.get("max_results", "25")),
        }
        return _dump(await self._get("/rest/api/3/search/jql", params))

    # --- transport -------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.get(path, params=params)
        return _body(response)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(path, json=payload)
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
        msg = f"invalid Jira identifier {text!r}"
        raise ValueError(msg)
    return text


def _projects_params(args: dict[str, Any]) -> dict[str, str]:
    query = str(args.get("query", ""))
    return {"query": query} if query else {}


def _create_issue_body(args: dict[str, Any]) -> dict[str, Any]:
    """The v3 create payload. Description is Atlassian Document Format, not text."""
    fields: dict[str, Any] = {
        "project": {"key": str(args["project_key"])},
        "summary": str(args["summary"]),
        "issuetype": {"name": str(args.get("issue_type", "Task"))},
    }
    description = str(args.get("description", ""))
    if description:
        fields["description"] = _document(description)
    return {"fields": fields}


def _document(text: str) -> dict[str, Any]:
    """Plain text as an Atlassian Document Format paragraph — what v3 accepts."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


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


def build_native_attachment(context: dict[str, Any]) -> JiraAttachment:
    """The fixed factory Arc calls to build this extension's attachment."""
    return JiraAttachment(
        base_url=_setting(context, "base_url", ENV_BASE_URL),
        email=_setting(context, "email", ENV_EMAIL),
        api_token=_setting(context, "api_token", ENV_API_TOKEN),
    )
