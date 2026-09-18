"""Decorator-form mcp_server module (SPEC-082, COMP-001).

A single ``@capability`` class :class:`McpServerDoor` owns the read-only
:class:`~arcagent.modules.mcp_server.server.McpServer` lifecycle: it is built from
the agent's configured tool registry on setup and dropped on teardown. Runtime
state lives in :mod:`arcagent.modules.mcp_server._runtime`; the agent calls
``_runtime.configure`` once at startup and this class reads state lazily.

This capability builds only the in-process read-only listing surface
(:class:`~arcagent.modules.mcp_server.server.McpServer`). It never binds a socket
— arcagent is headless. The full serving door (transports + the
verify→enroll→allowlist→dispatch→audit call path) is assembled by
:func:`~arcagent.modules.mcp_server.serving.build_door_from_agent` and bound to a
socket by ``arc mcp serve``.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.modules.mcp_server import _runtime
from arcagent.modules.mcp_server.server import McpServer
from arcagent.tools._decorator import capability

_logger = logging.getLogger("arcagent.modules.mcp_server.capabilities")


@capability(name="mcp_server")
class McpServerDoor:
    """Lifecycle-bound :class:`McpServer` wrapper.

    ``setup()`` builds the read-only server from the configured tool registry.
    ``teardown()`` drops it. Both are idempotent so a re-setup or a teardown
    without a prior setup is safe.
    """

    async def setup(self, ctx: Any) -> None:
        del ctx  # Loader passes None; state lives in _runtime.
        st = _runtime.state()
        if st.server is not None:
            return  # Idempotent: already set up.
        if st.tool_registry is None:
            _logger.warning("mcp_server enabled but no tool registry configured; door idle")
            return
        st.server = McpServer(
            st.tool_registry,
            server_name=st.config.server_name,
            page_size=st.config.page_size,
        )
        _logger.info("mcp_server door ready (read-only listing surface)")

    async def teardown(self) -> None:
        st = _runtime.state()
        st.server = None


__all__ = ["McpServerDoor"]
