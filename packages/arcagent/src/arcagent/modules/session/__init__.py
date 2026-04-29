"""Session module — JSONL store wrapper + SQLite FTS5 indexer + session_search tool.

Architecture (SDD §3.2):
  - store.py    : thin helpers for reading JSONL files without duplicating
                  core.session_manager logic
  - index.py    : SessionIndex — polling indexer (crash-safe, byte-offset checkpoint)
  - search.py   : session_search tool registered via tool_registry
  - identity_graph.py : IdentityGraph — cross-platform user identity resolution

The module follows the startup/shutdown protocol used by all arcagent modules.
SessionIndex is started at module startup and stopped at shutdown.  The
session_search tool is registered into the agent's ToolRegistry at startup.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from arcagent.core.module_bus import ModuleContext
from arcagent.modules.session.identity_graph import IdentityGraph, Link
from arcagent.modules.session.index import SearchHit, SessionIndex
from arcagent.modules.session.search import build_session_search_tool, set_index
from arcagent.modules.session.store import iter_session_files, read_messages_from_offset

_logger = logging.getLogger("arcagent.modules.session")

# Default poll interval (seconds).  Tests override via config.
_DEFAULT_POLL_INTERVAL = 30.0


class SessionModule:
    """Module that manages the FTS5 session index and registers session_search.

    Lifecycle:
      startup()  — opens DB, starts SessionIndex poll loop, registers tool
      shutdown() — stops poll loop, closes DB
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        workspace: Path = Path("."),
        **_kw: Any,
    ) -> None:
        cfg = config or {}
        self._workspace = workspace
        self._poll_interval: float = float(cfg.get("poll_interval", _DEFAULT_POLL_INTERVAL))
        self._index: SessionIndex | None = None

    @property
    def name(self) -> str:
        return "session"

    async def startup(self, ctx: ModuleContext) -> None:
        """Start the index poll loop and register the session_search tool."""
        sessions_dir = self._workspace / "sessions"
        db_path = sessions_dir / "index.db"

        self._index = SessionIndex(
            db_path=db_path,
            sessions_dir=sessions_dir,
            poll_interval=self._poll_interval,
        )
        await self._index.start()

        # Wire index into the search module so the tool callable can reach it.
        set_index(self._index)

        # Register tool with the agent's tool registry.
        tool = build_session_search_tool()
        ctx.tool_registry.register(tool)

        _logger.info(
            "Session module started: db=%s poll_interval=%.1fs",
            db_path,
            self._poll_interval,
        )

    async def shutdown(self) -> None:
        """Stop the poll loop and close the database."""
        if self._index is not None:
            await self._index.stop()
            self._index = None
        # Unset the global index reference so subsequent calls fail clearly.
        set_index(None)
        _logger.info("Session module stopped")


__all__ = [
    "IdentityGraph",
    "Link",
    "SearchHit",
    "SessionIndex",
    "SessionModule",
    "build_session_search_tool",
    "iter_session_files",
    "read_messages_from_offset",
]
