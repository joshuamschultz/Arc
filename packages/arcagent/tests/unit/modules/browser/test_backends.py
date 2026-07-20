"""Tests for the browser backend seam.

Covers:
  - build_backend maps ``provider`` → the right backend and enforces the
    federal remote-browser rule at selection time.
  - CDPBackend opens/closes a CDPClientManager.
  - BrowserbaseBackend's session lifecycle (create → attach → release),
    with the HTTP + CDP layers mocked (no live Browserbase account).
  - CDPClientManager's page-target attach + sessionId routing used by
    browser-level endpoints (Browserbase, Steel, Browserless).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from arcagent.modules.browser.backends import (
    BrowserbaseBackend,
    CDPBackend,
    build_backend,
)
from arcagent.modules.browser.backends.select import UnknownBackendError
from arcagent.modules.browser.cdp_client import CDPClientManager
from arcagent.modules.browser.config import (
    BrowserbaseConfig,
    BrowserConfig,
    BrowserConnectionConfig,
)
from arcagent.modules.browser.errors import CDPConnectionError, LocalBrowserNotAllowedError


class TestBuildBackend:
    """Provider selection and federal gating."""

    def test_default_provider_is_cdp(self) -> None:
        backend = build_backend(BrowserConfig())
        assert isinstance(backend, CDPBackend)
        assert backend.name == "cdp"

    def test_browserbase_provider_selected(self) -> None:
        cfg = BrowserConfig(provider="browserbase", browserbase={"project_id": "p1"})  # type: ignore[arg-type]
        backend = build_backend(cfg)
        assert isinstance(backend, BrowserbaseBackend)
        assert backend.name == "browserbase"

    def test_federal_local_cdp_rejected(self) -> None:
        """Federal + cdp with no remote endpoint must fail before any launch."""
        cfg = BrowserConfig(tier="federal")  # empty cdp_url → local launch
        with pytest.raises(LocalBrowserNotAllowedError):
            build_backend(cfg)

    def test_federal_remote_cdp_allowed(self) -> None:
        cfg = BrowserConfig(
            tier="federal",
            connection={"cdp_url": "ws://sandbox.internal:9222/devtools/browser/x"},  # type: ignore[arg-type]
        )
        backend = build_backend(cfg)
        assert isinstance(backend, CDPBackend)

    def test_unknown_provider_raises(self) -> None:
        cfg = BrowserConfig()
        object.__setattr__(cfg, "provider", "bogus")
        with pytest.raises(UnknownBackendError):
            build_backend(cfg)


@pytest.mark.asyncio
class TestCDPBackend:
    """The default backend owns a CDPClientManager lifecycle."""

    async def test_open_connects_and_returns_session(self) -> None:
        backend = CDPBackend(BrowserConnectionConfig())
        with patch("arcagent.modules.browser.backends.cdp.CDPClientManager") as cls:
            mock_client = AsyncMock()
            mock_client.connect = AsyncMock()
            cls.return_value = mock_client

            session = await backend.open()

            cls.assert_called_once()
            mock_client.connect.assert_awaited_once()
            assert session is mock_client

    async def test_close_disconnects(self) -> None:
        backend = CDPBackend(BrowserConnectionConfig())
        with patch("arcagent.modules.browser.backends.cdp.CDPClientManager") as cls:
            mock_client = AsyncMock()
            cls.return_value = mock_client
            await backend.open()
            await backend.close()
            mock_client.disconnect.assert_awaited_once()

    async def test_close_without_open_is_safe(self) -> None:
        backend = CDPBackend(BrowserConnectionConfig())
        await backend.close()  # must not raise


@pytest.mark.asyncio
class TestBrowserbaseBackend:
    """Managed session create → attach → release, all deps mocked."""

    def _config(self) -> BrowserbaseConfig:
        return BrowserbaseConfig(project_id="proj-123")

    async def test_open_creates_session_and_attaches(self) -> None:
        backend = BrowserbaseBackend(self._config(), tier="enterprise")

        with (
            patch(
                "arcagent.core.vault.resolver.resolve_secret",
                new=AsyncMock(return_value="bb-key"),
            ),
            patch.object(
                BrowserbaseBackend,
                "_post",
                new=AsyncMock(
                    return_value={"id": "sess-1", "connectUrl": "wss://connect.browserbase.com/x"}
                ),
            ),
            patch(
                "arcagent.modules.browser.backends.browserbase.CDPClientManager"
            ) as cdp_cls,
        ):
            mock_client = AsyncMock()
            mock_client.connect = AsyncMock()
            cdp_cls.return_value = mock_client

            session = await backend.open()

            # CDP client is pointed at the connectUrl as a browser endpoint.
            conn = cdp_cls.call_args.args[0]
            assert conn.cdp_url == "wss://connect.browserbase.com/x"
            assert conn.endpoint_kind == "browser"
            mock_client.connect.assert_awaited_once()
            assert session is mock_client

    async def test_open_without_project_id_raises(self) -> None:
        backend = BrowserbaseBackend(BrowserbaseConfig(), tier="enterprise")
        with pytest.raises(CDPConnectionError, match="project_id"):
            await backend.open()

    async def test_open_missing_connect_url_raises(self) -> None:
        backend = BrowserbaseBackend(self._config(), tier="enterprise")
        with (
            patch(
                "arcagent.core.vault.resolver.resolve_secret",
                new=AsyncMock(return_value="bb-key"),
            ),
            patch.object(
                BrowserbaseBackend, "_post", new=AsyncMock(return_value={"id": "sess-1"})
            ),
        ):
            with pytest.raises(CDPConnectionError, match="connectUrl"):
                await backend.open()

    async def test_close_disconnects_and_releases(self) -> None:
        backend = BrowserbaseBackend(self._config(), tier="enterprise")
        mock_client = AsyncMock()
        backend._client = mock_client
        backend._session_id = "sess-1"

        post = AsyncMock(return_value={})
        with patch.object(BrowserbaseBackend, "_post", new=post):
            await backend.close()

        mock_client.disconnect.assert_awaited_once()
        # Release call carries the REQUEST_RELEASE status.
        url, body = post.call_args.args
        assert "sess-1" in url
        assert body["status"] == "REQUEST_RELEASE"

    async def test_close_release_failure_does_not_raise(self) -> None:
        backend = BrowserbaseBackend(self._config(), tier="enterprise")
        backend._client = AsyncMock()
        backend._session_id = "sess-1"
        with patch.object(
            BrowserbaseBackend, "_post", new=AsyncMock(side_effect=RuntimeError("boom"))
        ):
            await backend.close()  # teardown must swallow the error


class _FakeWS:
    """Minimal CDP WebSocket double: echoes each command's id."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        last = self.sent[-1]
        return json.dumps({"id": last["id"], "result": {"ok": True}})

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
class TestCDPSessionRouting:
    """Browser-level endpoints attach a page target and route by sessionId."""

    async def test_send_includes_session_id_when_set(self) -> None:
        mgr = CDPClientManager(BrowserConnectionConfig())
        mgr._ws = _FakeWS()
        mgr._connected = True
        mgr._session_id = "SESS-42"

        await mgr.send("Page", "navigate", {"url": "https://example.com"})

        assert mgr._ws.sent[-1]["sessionId"] == "SESS-42"

    async def test_send_omits_session_id_when_unset(self) -> None:
        mgr = CDPClientManager(BrowserConnectionConfig())
        mgr._ws = _FakeWS()
        mgr._connected = True

        await mgr.send("Page", "navigate", {"url": "https://example.com"})

        assert "sessionId" not in mgr._ws.sent[-1]

    async def test_attach_uses_existing_page_target(self) -> None:
        mgr = CDPClientManager(BrowserConnectionConfig(endpoint_kind="browser"))
        mgr._connected = True
        calls: list[tuple[str, str]] = []

        async def fake_send(domain: str, method: str, params: Any = None) -> dict[str, Any]:
            calls.append((domain, method))
            if method == "getTargets":
                return {"targetInfos": [{"type": "page", "targetId": "T1"}]}
            if method == "attachToTarget":
                return {"sessionId": "SESS-1"}
            return {}

        mgr.send = fake_send  # type: ignore[method-assign]
        await mgr._attach_to_page_target()

        assert mgr._session_id == "SESS-1"
        assert ("Target", "createTarget") not in calls  # a page already existed

    async def test_attach_creates_page_when_none(self) -> None:
        mgr = CDPClientManager(BrowserConnectionConfig(endpoint_kind="browser"))
        mgr._connected = True

        async def fake_send(domain: str, method: str, params: Any = None) -> dict[str, Any]:
            if method == "getTargets":
                return {"targetInfos": []}
            if method == "createTarget":
                return {"targetId": "T-new"}
            if method == "attachToTarget":
                return {"sessionId": "SESS-2"}
            return {}

        mgr.send = fake_send  # type: ignore[method-assign]
        await mgr._attach_to_page_target()

        assert mgr._session_id == "SESS-2"

    async def test_attach_no_session_id_raises(self) -> None:
        mgr = CDPClientManager(BrowserConnectionConfig(endpoint_kind="browser"))
        mgr._connected = True

        async def fake_send(domain: str, method: str, params: Any = None) -> dict[str, Any]:
            if method == "getTargets":
                return {"targetInfos": [{"type": "page", "targetId": "T1"}]}
            return {}  # attachToTarget returns no sessionId

        mgr.send = fake_send  # type: ignore[method-assign]
        with pytest.raises(CDPConnectionError, match="attach"):
            await mgr._attach_to_page_target()
