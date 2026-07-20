"""Managed browser backend: Browserbase (``provider = "browserbase"``).

Browserbase runs Chrome in its own sandbox and hands back a browser-level
CDP endpoint. This backend creates a session over the REST API, attaches
to it through the shared :class:`CDPClientManager` (which discovers a page
target for browser-level endpoints), and releases the session on close so
no remote browser is left running.

The API key never lives in config — it is resolved through the tier-aware
secret resolver, exactly like the web module's providers. This is the
"robust override" of the quick local default: same tools, a managed
browser, one config line plus a key.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.modules.browser.backends.protocols import BrowserSession
from arcagent.modules.browser.cdp_client import CDPClientManager
from arcagent.modules.browser.config import BrowserbaseConfig, BrowserConnectionConfig
from arcagent.modules.browser.errors import CDPConnectionError

_logger = logging.getLogger("arcagent.modules.browser.backends.browserbase")


class BrowserbaseBackend:
    """Create, attach to, and release a managed Browserbase session."""

    name = "browserbase"

    def __init__(self, config: BrowserbaseConfig, *, tier: str) -> None:
        self._config = config
        self._tier = tier
        self._client: CDPClientManager | None = None
        self._session_id = ""
        self._api_key = ""

    async def open(self) -> BrowserSession:
        """Create a Browserbase session and attach a CDP client to it."""
        if not self._config.project_id:
            raise CDPConnectionError(
                message="Browserbase provider requires browserbase.project_id",
                details={"tier": self._tier},
            )
        self._api_key = await self._resolve_api_key()
        connect_url, session_id = await self._create_session()
        self._session_id = session_id

        client = CDPClientManager(
            BrowserConnectionConfig(cdp_url=connect_url, endpoint_kind="browser")
        )
        await client.connect()
        self._client = client
        _logger.info("Browserbase session %s connected", session_id)
        return client

    async def close(self) -> None:
        """Disconnect the CDP client and release the Browserbase session."""
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
        if self._session_id:
            await self._release_session(self._session_id)
            self._session_id = ""

    async def _resolve_api_key(self) -> str:
        """Resolve the Browserbase API key via the tier-aware resolver."""
        from arcagent.core.vault.resolver import resolve_secret

        key: str = await resolve_secret(
            "browserbase_api_key",
            tier=self._tier,
            backend=None,
            env_fallback_var=self._config.api_key_env,
        )
        return key

    async def _create_session(self) -> tuple[str, str]:
        """POST a new session; return ``(connect_url, session_id)``."""
        body: dict[str, Any] = {"projectId": self._config.project_id}
        if self._config.region:
            body["region"] = self._config.region
        if self._config.proxies:
            body["proxies"] = True
        if self._config.keep_alive:
            body["keepAlive"] = True

        data = await self._post(f"{self._config.api_base}/sessions", body)
        connect_url = data.get("connectUrl", "")
        session_id = data.get("id", "")
        if not connect_url or not session_id:
            raise CDPConnectionError(
                message="Browserbase session response missing connectUrl/id",
                details={"keys": sorted(data)},
            )
        return connect_url, session_id

    async def _release_session(self, session_id: str) -> None:
        """Best-effort session release — a lingering session bills and holds a browser."""
        try:
            await self._post(
                f"{self._config.api_base}/sessions/{session_id}",
                {"projectId": self._config.project_id, "status": "REQUEST_RELEASE"},
            )
        except Exception:  # reason: teardown must not raise — log and move on
            _logger.warning("Browserbase session %s release failed", session_id, exc_info=True)

    async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to the Browserbase API with the API-key header."""
        import httpx

        async with httpx.AsyncClient(timeout=self._config.request_timeout_s) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"X-BB-API-Key": self._api_key, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result
