"""session_search tool — full-text search over the agent's past sessions.

This module exposes the ``session_search`` function which is registered with
arcagent's tool registry via the ``native_tool`` decorator.  It delegates to
``SessionIndex.search()`` and returns a JSON-serializable list.

M2 TODO: Top-N hits should be passed to an auxiliary arcllm call for
summarization before injection into the agent's context window (token
efficiency).  The slot for that call is marked below.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport

_logger = logging.getLogger("arcagent.modules.session.search")

# Module-level reference set by SessionModule.startup() so the tool can
# reach the live SessionIndex without a circular import.
_index: Any = None  # SessionIndex instance set at startup


def set_index(index: Any) -> None:
    """Wire the live SessionIndex into this module's tool callable."""
    global _index
    _index = index


async def session_search(
    query: str = "",
    limit: int = 20,
    since: str = "",
    classification_max: str = "",
    **_kwargs: Any,
) -> list[dict[str, Any]]:
    """Search the agent's past sessions by full-text query.

    Returns ranked snippets.

    Parameters
    ----------
    query:
        Full-text search query string.  Supports FTS5 syntax (phrases,
        boolean operators, NEAR).
    limit:
        Maximum number of results to return.  Defaults to 20.
    since:
        ISO-8601 datetime string.  If provided, only messages after this
        timestamp are returned.
    classification_max:
        ACL filter stub.  Accepted values: 'unclassified', 'cui', 'secret'.
        Messages above this level are excluded.  Full ACL enforcement is
        M2 work; this parameter exercises the query path now.
    """
    if _index is None:
        _logger.warning("session_search called but SessionIndex is not initialized")
        return []

    if not query:
        return []

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=UTC)
        except ValueError:
            _logger.warning("Invalid 'since' value %r; ignoring filter", since)

    cl_max = classification_max or None

    hits = _index.search(
        q=query,
        limit=limit,
        since=since_dt,
        classification_max=cl_max,
    )

    # M2 work: top-N hits get aux-LLM summarization before injection.
    # When implemented, call arcllm here to produce a compact summary of
    # the retrieved snippets to reduce token usage before they are placed
    # into the agent's context window.

    results = [h.model_dump() for h in hits]
    _logger.info(
        "session.search.queried: query=%r limit=%d since=%s hits=%d",
        query,
        limit,
        since or "none",
        len(results),
    )
    return results


def build_session_search_tool() -> RegisteredTool:
    """Build and return the session_search RegisteredTool instance."""
    return RegisteredTool(
        name="session_search",
        description=(
            "Search the agent's past sessions by full-text query. "
            "Returns ranked snippets."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Full-text search query. Supports FTS5 syntax: "
                        "phrases in quotes, AND/OR/NOT operators, NEAR(word1 word2)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (default 20).",
                    "default": 20,
                },
                "since": {
                    "type": "string",
                    "description": "ISO-8601 datetime; only messages after this time.",
                },
                "classification_max": {
                    "type": "string",
                    "enum": ["unclassified", "cui", "secret"],
                    "description": (
                        "ACL filter: exclude messages above this classification "
                        "level. Full ACL enforcement is M2 work."
                    ),
                },
            },
            "required": ["query"],
        },
        transport=ToolTransport.NATIVE,
        execute=session_search,
        timeout_seconds=10,
        source="session",
        category="recall",
        when_to_use=(
            "Use when you need to recall information from earlier conversations "
            "or find past discussions about a topic."
        ),
        classification="read_only",
    )
