"""Decorator-form session module — SPEC-021 capability surface.

A single ``@capability`` class :class:`SessionIndexCapability` owns the
:class:`SessionIndex` and :class:`IdentityGraph` lifecycle (open on setup,
stop + close on teardown). One module-level ``@tool`` function exposes the
``session_search`` surface the LLM uses to query past sessions.

Runtime state lives in :mod:`arcagent.modules.session._runtime`. The agent
calls :func:`_runtime.configure` once at startup; the capability class and
tool both read state lazily.

Why a module-level tool instead of a method on :class:`SessionIndexCapability`?
The loader's :class:`CapabilityClassMetadata` path instantiates the class with
no arguments and registers any ``@tool``-stamped methods bound to that instance.
The search tool only needs the live ``SessionIndex`` from ``_runtime.state()``
— going through the class instance adds an indirection that buys nothing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from arcagent.modules.session import _runtime
from arcagent.modules.session.identity_graph import IdentityGraph
from arcagent.modules.session.index import SessionIndex
from arcagent.tools._decorator import capability, tool

_logger = logging.getLogger("arcagent.modules.session.capabilities")


@capability(name="session_index")
class SessionIndexCapability:
    """Lifecycle-bound :class:`SessionIndex` and :class:`IdentityGraph` wrapper.

    ``setup()`` constructs both objects against the configured workspace,
    initialises the FTS5 schema, and starts the background poll loop.

    ``teardown()`` stops the poll loop and closes the identity graph
    connection. Both operations are idempotent.
    """

    async def setup(self, ctx: Any) -> None:
        """Open DB, start SessionIndex poll loop, open IdentityGraph."""
        del ctx  # Loader passes None; state lives in _runtime.
        st = _runtime.state()
        if st.index is not None:
            return  # Idempotent: already set up.

        sessions_dir = st.workspace / "sessions"
        db_path = sessions_dir / "index.db"

        index = SessionIndex(
            db_path=db_path,
            sessions_dir=sessions_dir,
            poll_interval=st.poll_interval,
        )
        await index.start()
        st.index = index

        identity_graph = IdentityGraph(db_path=db_path)
        st.identity_graph = identity_graph

        _logger.info(
            "SessionIndex capability started: db=%s poll_interval=%.1fs",
            db_path,
            st.poll_interval,
        )

    async def teardown(self) -> None:
        """Stop poll loop and close identity graph. Idempotent."""
        st = _runtime.state()
        if st.index is not None:
            await st.index.stop()
            st.index = None
        if st.identity_graph is not None:
            st.identity_graph.close()
            st.identity_graph = None
        _logger.info("SessionIndex capability stopped")


# --- Tools -------------------------------------------------------------------


@tool(
    name="session_search",
    description=("Search the agent's past sessions by full-text query. Returns ranked snippets."),
    classification="read_only",
    capability_tags=("recall",),
    when_to_use=(
        "Use when you need to recall information from earlier conversations "
        "or find past discussions about a topic."
    ),
)
async def session_search(
    query: str = "",
    limit: int = 20,
    since: str = "",
    classification_max: str = "",
) -> str:
    """Search the agent's past sessions by full-text query.

    Parameters
    ----------
    query:
        Full-text search query. Supports FTS5 syntax: phrases in quotes,
        AND/OR/NOT operators, NEAR(word1 word2).
    limit:
        Maximum number of results to return. Defaults to 20.
    since:
        ISO-8601 datetime string. Only messages after this timestamp are
        returned.
    classification_max:
        ACL filter. Accepted values: 'unclassified', 'cui', 'secret'.
        Messages above this level are excluded.
    """
    import json

    st = _runtime.state()

    if st.index is None:
        _logger.warning("session_search called but SessionIndex is not started")
        return json.dumps([])

    if not query:
        return json.dumps([])

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=UTC)
        except ValueError:
            _logger.warning("Invalid 'since' value %r; ignoring filter", since)

    cl_max = classification_max or None

    hits = st.index.search(
        q=query,
        limit=limit,
        since=since_dt,
        classification_max=cl_max,
    )

    results = [h.model_dump() for h in hits]
    _logger.info(
        "session.search.queried: query=%r limit=%d since=%s hits=%d",
        query,
        limit,
        since or "none",
        len(results),
    )
    return json.dumps(results)


__all__ = [
    "SessionIndexCapability",
    "session_search",
]
