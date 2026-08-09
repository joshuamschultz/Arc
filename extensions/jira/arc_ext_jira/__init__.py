"""Jira Cloud REST v3 adapter — the whole third-party side of the jira bundle.

D-562: no third-party Atlassian server is adopted, so this folder owns the
transport. It imports nothing from Arc except the hook's own value types, which
is the property that makes it deletable: remove the bundle and this code, its
knowledge of Jira, and its dependency all go with it.

**Credentials.** The ``[[secrets]]`` this bundle declares are resolved from Arc's
secret store for this connected instance and handed to
:func:`build_native_attachment` in its context. That is the only way one reaches
this adapter: there is no environment fallback, because an ``ARC_JIRA_*``
variable is not scoped to an instance — two Jira accounts on one agent would
silently share it — and a value arriving from the process environment would
bypass the audited, tier-selected store entirely. When a credential is absent,
:meth:`JiraAttachment.probe` refuses by name rather than failing later with a
401 nobody can read.

Two rules shape every request. Every call is bounded by an explicit timeout, so a
hung Atlassian endpoint cannot hold a turn open. And a JQL string or an issue key
is data: it travels as a query parameter or a JSON value that httpx encodes, and
is never concatenated into a path.
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
                    f"jira has no credential for {', '.join(missing)} — "
                    f"run 'arc connector auth <instance>' to supply them."
                ),
            )
        try:
            body = await self._get("/rest/api/3/myself", {})
        except httpx.HTTPStatusError as exc:
            return ProbeResult(
                reachable=False, detail=_refused(exc.response.status_code, self._base_url)
            )
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


def _refused(status: int, base_url: str) -> str:
    """What the operator should do about the status Atlassian answered the probe with.

    httpx's own sentence — ``Client error '401 Unauthorized' for url … For more
    information check <MDN>`` — was what an operator saw after pasting a token. It
    names no field and no next step, and a reference page about HTTP status codes
    is not an instruction.

    The three that matter need three different actions, so they must not read
    alike. 401 on ``/myself`` means the email and the token are not one account (or
    the token is revoked) — both fields, and a valid token can still be the wrong
    one. 403 means the sign-in worked and the account may not use this API, which
    reissuing a perfectly good token would not fix. 404 means the address is not a
    Jira site at all, which is a third field entirely.
    """
    if status == 401:
        return (
            "Atlassian refused the email and the API token together. They have to belong "
            "to the same account: check the email is the one you sign in to Atlassian "
            "with, and if the token may have been revoked, create a new one at "
            "id.atlassian.com/manage-profile/security/api-tokens."
        )
    if status == 403:
        return (
            f"Atlassian accepted the sign-in, but this account is not permitted to use "
            f"the Jira API on {base_url}. Ask a site administrator to give it access."
        )
    if status == 404:
        return (
            f"{base_url} answered, but there is no Jira there. Check the address is the "
            f"one your browser bar shows when you are looking at Jira; it usually ends "
            f"in .atlassian.net."
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


def _credential(context: dict[str, Any], key: str) -> str:
    """One declared credential out of the context Arc resolved from its secret store."""
    return str(context.get(key) or "")


def build_native_attachment(context: dict[str, Any]) -> JiraAttachment:
    """The fixed factory Arc calls to build this extension's attachment."""
    return JiraAttachment(
        base_url=_credential(context, "base_url"),
        email=_credential(context, "email"),
        api_token=_credential(context, "api_token"),
    )
