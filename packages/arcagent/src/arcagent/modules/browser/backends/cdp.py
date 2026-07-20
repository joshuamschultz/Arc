"""Default browser backend: raw Chrome DevTools Protocol.

Covers both the quick local default (empty ``cdp_url`` → launch a
headless Chrome subprocess) and generic remote attach (``cdp_url`` set →
connect to an already-running, separately-sandboxed browser). This is
the zero-dependency, federal-safe backend: no third party sits inside
the trust boundary.

Local vs remote is a property of the connection config, not two classes
— :class:`~arcagent.modules.browser.cdp_client.CDPClientManager` already
branches on ``cdp_url``. This backend just owns that manager's lifecycle
behind the :class:`BrowserBackend` seam.
"""

from __future__ import annotations

from arcagent.modules.browser.backends.protocols import BrowserSession
from arcagent.modules.browser.cdp_client import CDPClientManager
from arcagent.modules.browser.config import BrowserConnectionConfig


class CDPBackend:
    """Launch or attach to a Chrome browser over raw CDP."""

    name = "cdp"

    def __init__(self, connection: BrowserConnectionConfig) -> None:
        self._connection = connection
        self._client: CDPClientManager | None = None

    async def open(self) -> BrowserSession:
        """Connect the CDP client and return it as the live session.

        With an empty ``cdp_url`` this launches a local headless Chrome;
        with a ``cdp_url`` it attaches to that remote endpoint.
        """
        client = CDPClientManager(self._connection)
        await client.connect()
        self._client = client
        return client

    async def close(self) -> None:
        """Disconnect and reap the Chrome process (safe if never opened)."""
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
