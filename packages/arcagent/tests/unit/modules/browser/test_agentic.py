"""Tests for the agentic ``browser_task`` tool.

Verifies governance (federal forbidden, off-by-default, loud degrade when
the optional extra is absent) and that the tool registers. The live agent
run itself requires the ``browser-use`` package + a browser + LLM creds and
is not exercised here.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.browser import _runtime
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.errors import (
    AgenticBrowserForbiddenError,
    BrowserUseNotInstalledError,
    CapabilityDisabledError,
)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


def _configure(**cfg: object) -> None:
    bus = MagicMock()
    bus.emit = AsyncMock()
    _runtime.configure(config=BrowserConfig(**cfg), bus=bus)  # type: ignore[arg-type]


@pytest.mark.asyncio
class TestBrowserTaskGating:
    async def test_federal_is_forbidden(self) -> None:
        # Federal + remote endpoint so config itself is valid; the tool still refuses.
        _configure(tier="federal", browser_use={"enabled": True})
        from arcagent.modules.browser.agentic import browser_task

        with pytest.raises(AgenticBrowserForbiddenError):
            await browser_task("find the docs")

    async def test_disabled_by_default(self) -> None:
        _configure(tier="personal")
        from arcagent.modules.browser.agentic import browser_task

        with pytest.raises(CapabilityDisabledError):
            await browser_task("find the docs")

    async def test_enabled_but_extra_missing_degrades_loud(self) -> None:
        """With browser-use NOT installed, an enabled call names the fix."""
        _configure(tier="personal", browser_use={"enabled": True})
        from arcagent.modules.browser.agentic import browser_task

        with pytest.raises(BrowserUseNotInstalledError, match="pip install browser-use"):
            await browser_task("find the docs")

    async def test_unconfigured_raises(self) -> None:
        from arcagent.modules.browser.agentic import browser_task

        with pytest.raises(RuntimeError, match="before runtime is configured"):
            await browser_task("find the docs")


@pytest.mark.asyncio
async def test_browser_task_registers(tmp_path: Path) -> None:
    """The loader picks up browser_task from the top-level agentic.py."""
    from arcagent.capabilities.capability_loader import CapabilityLoader
    from arcagent.capabilities.capability_registry import CapabilityRegistry
    from arcagent.modules.browser import agentic

    scan_dir = tmp_path / "browser_scan"
    scan_dir.mkdir()
    (scan_dir / "agentic.py").symlink_to(Path(agentic.__file__))

    reg = CapabilityRegistry()
    loader = CapabilityLoader(scan_roots=[("browser", scan_dir)], registry=reg)
    await loader.scan_and_register()

    entry = await reg.get_tool("browser_task")
    assert entry is not None
    assert entry.meta.kind == "tool"
