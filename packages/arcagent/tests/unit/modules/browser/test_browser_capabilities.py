"""SPEC-021 Task 3.3 — browser module decorator-form tests.

The new ``capabilities.py`` exposes one ``@capability(name="browser")``
class plus N module-level ``@tool`` functions. This file verifies:

  1. The capability class registers as a :class:`LifecycleEntry`.
  2. The browser tools register as :class:`ToolEntry` instances.
  3. ``BrowserCapability.setup()`` connects the CDP client and creates
     the accessibility manager.
  4. ``BrowserCapability.teardown()`` cleanly closes the CDP client
     (Chrome process is reaped via ``CDPClientManager.disconnect``).
  5. Tool functions raise :class:`RuntimeError` when called before
     ``setup()`` runs.

Legacy :class:`BrowserModule` tests continue to verify behaviour at the
wrapper level.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.capability_loader import CapabilityLoader
from arcagent.core.capability_registry import CapabilityRegistry
from arcagent.modules.browser import _runtime
from arcagent.modules.browser.config import BrowserConfig


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bus = MagicMock()
    bus.emit = AsyncMock()
    _runtime.configure(
        config=BrowserConfig(),
        workspace=workspace,
        bus=bus,
        telemetry=MagicMock(),
    )
    return workspace


@pytest.mark.asyncio
class TestLoaderRegistration:
    """Capability loader picks up the @capability class + @tool functions."""

    async def test_capability_class_registers_as_lifecycle_entry(self, tmp_path: Path) -> None:
        from arcagent.modules.browser import capabilities as browser_caps

        # Loader scans for files in the directory that contain stamped
        # callables. Point it directly at the capabilities.py file's
        # parent so we pick up ONLY the new decorator surface.
        module_dir = Path(browser_caps.__file__).parent
        # Filter the scan to a temp dir containing only capabilities.py
        # so we don't pull in legacy browser_module.py side effects.
        scan_dir = tmp_path / "browser_scan"
        scan_dir.mkdir()
        (scan_dir / "capabilities.py").symlink_to(module_dir / "capabilities.py")

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("browser", scan_dir)], registry=reg)
        await loader.scan_and_register()

        cap_entry = await reg.get_capability("browser")
        assert cap_entry is not None
        assert cap_entry.meta.kind == "capability"
        assert cap_entry.meta.name == "browser"

    async def test_browser_tools_register(self, tmp_path: Path) -> None:
        from arcagent.modules.browser import capabilities as browser_caps

        module_dir = Path(browser_caps.__file__).parent
        scan_dir = tmp_path / "browser_scan"
        scan_dir.mkdir()
        (scan_dir / "capabilities.py").symlink_to(module_dir / "capabilities.py")

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("browser", scan_dir)], registry=reg)
        await loader.scan_and_register()

        # Spot-check a representative subset of tools across categories.
        for tool_name in (
            "browser_navigate",
            "browser_go_back",
            "browser_screenshot",
            "browser_click",
            "browser_read_page",
            "browser_handle_dialog",
            "browser_get_cookies",
        ):
            entry = await reg.get_tool(tool_name)
            assert entry is not None, f"missing tool {tool_name}"
            assert entry.meta.kind == "tool"


@pytest.mark.asyncio
class TestBrowserCapabilityLifecycle:
    """setup() connects CDP and creates the AX manager; teardown() disconnects."""

    async def test_setup_connects_cdp_and_creates_ax(self, configured: Path) -> None:
        from arcagent.modules.browser.capabilities import BrowserCapability

        cap = BrowserCapability()

        with patch("arcagent.modules.browser.capabilities.CDPClientManager") as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.url = "ws://localhost:9222/devtools/browser/abc"
            mock_cdp_cls.return_value = mock_cdp

            await cap.setup(None)

            mock_cdp_cls.assert_called_once()
            mock_cdp.connect.assert_awaited_once()
            st = _runtime.state()
            assert st.cdp_client is mock_cdp
            assert st.ax_manager is not None

    async def test_setup_emits_connected_event(self, configured: Path) -> None:
        from arcagent.modules.browser.capabilities import BrowserCapability

        cap = BrowserCapability()
        bus = _runtime.state().bus

        with patch("arcagent.modules.browser.capabilities.CDPClientManager") as mock_cdp_cls:
            mock_cdp = AsyncMock()
            mock_cdp.connect = AsyncMock()
            mock_cdp.url = "ws://localhost:9222/devtools/browser/abc"
            mock_cdp_cls.return_value = mock_cdp

            await cap.setup(None)

        bus.emit.assert_any_await(
            "browser.connected",
            {"cdp_url": "ws://localhost:9222/devtools/browser/abc"},
        )

    async def test_teardown_disconnects_cdp(self, configured: Path) -> None:
        """Chrome process must cleanly shut down on capability teardown."""
        from arcagent.modules.browser.capabilities import BrowserCapability

        cap = BrowserCapability()
        mock_cdp = AsyncMock()
        mock_cdp.disconnect = AsyncMock()
        mock_cdp.url = "ws://localhost:9222/devtools/browser/abc"

        # Inject a "connected" CDP client and AX manager directly so we
        # don't have to mock the whole connect path again.
        st = _runtime.state()
        st.cdp_client = mock_cdp
        st.ax_manager = MagicMock()

        await cap.teardown()

        mock_cdp.disconnect.assert_awaited_once()
        assert st.cdp_client is None
        assert st.ax_manager is None

    async def test_teardown_emits_disconnected_event(self, configured: Path) -> None:
        from arcagent.modules.browser.capabilities import BrowserCapability

        cap = BrowserCapability()
        bus = _runtime.state().bus
        mock_cdp = AsyncMock()
        mock_cdp.disconnect = AsyncMock()
        st = _runtime.state()
        st.cdp_client = mock_cdp
        st.ax_manager = MagicMock()

        await cap.teardown()

        bus.emit.assert_any_await("browser.disconnected", {})

    async def test_teardown_without_setup_is_safe(self, configured: Path) -> None:
        """teardown() doesn't raise when CDP was never connected."""
        from arcagent.modules.browser.capabilities import BrowserCapability

        cap = BrowserCapability()
        # No CDP client set; should be a no-op disconnect path.
        await cap.teardown()
        # State stays cleared.
        st = _runtime.state()
        assert st.cdp_client is None
        assert st.ax_manager is None


@pytest.mark.asyncio
class TestRuntimeContract:
    """Runtime guards: tools raise if state isn't configured / set up."""

    async def test_unconfigured_raises(self) -> None:
        from arcagent.modules.browser.capabilities import browser_navigate

        with pytest.raises(RuntimeError, match="before runtime is configured"):
            await browser_navigate("https://example.com")

    async def test_configured_but_not_set_up_raises(self, configured: Path) -> None:
        """A tool called before BrowserCapability.setup() must fail loud."""
        from arcagent.modules.browser.capabilities import browser_screenshot

        with pytest.raises(RuntimeError, match="CDP client not initialised"):
            await browser_screenshot()


@pytest.mark.asyncio
class TestToolDelegation:
    """Tools delegate CDP calls to the runtime's CDP client."""

    async def test_browser_screenshot_calls_capture(self, configured: Path) -> None:
        from arcagent.modules.browser.capabilities import browser_screenshot

        mock_cdp = AsyncMock()
        mock_cdp.send = AsyncMock(return_value={"data": "iVBORw0KGgo="})
        st = _runtime.state()
        st.cdp_client = mock_cdp
        st.ax_manager = MagicMock()

        result = await browser_screenshot()
        mock_cdp.send.assert_awaited_once()
        domain, method, _params = mock_cdp.send.call_args.args
        assert domain == "Page"
        assert method == "captureScreenshot"
        assert "iVBORw0KGgo=" in result

    async def test_browser_reload_sends_page_reload(self, configured: Path) -> None:
        from arcagent.modules.browser.capabilities import browser_reload

        mock_cdp = AsyncMock()
        mock_cdp.send = AsyncMock(return_value={})
        st = _runtime.state()
        st.cdp_client = mock_cdp
        st.ax_manager = MagicMock()

        result = await browser_reload()
        mock_cdp.send.assert_awaited_once_with("Page", "reload")
        assert result == "Page reloaded"
