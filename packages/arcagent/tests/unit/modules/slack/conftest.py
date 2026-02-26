"""Shared fixtures for slack module tests — SPEC-011."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def make_ctx(tmp_path: Path) -> MagicMock:
    """Create a mock ModuleContext for slack tests."""
    ctx = MagicMock()
    ctx.bus = MagicMock()
    ctx.bus.subscribe = MagicMock()
    ctx.bus.emit = AsyncMock()
    ctx.tool_registry = MagicMock()
    ctx.tool_registry.register = MagicMock()
    ctx.workspace = tmp_path
    return ctx
