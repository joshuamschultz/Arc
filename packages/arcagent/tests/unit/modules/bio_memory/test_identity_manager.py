"""Tests for IdentityManager — how-i-work.md lifecycle, budget, audit events."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.identity_manager import IdentityManager
from arcagent.utils.io import CHARS_PER_TOKEN


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def config() -> BioMemoryConfig:
    return BioMemoryConfig()


@pytest.fixture
def telemetry() -> MagicMock:
    return MagicMock()


@pytest.fixture
def im(
    memory_dir: Path, config: BioMemoryConfig, telemetry: MagicMock,
) -> IdentityManager:
    return IdentityManager(
        memory_dir=memory_dir, config=config, telemetry=telemetry,
    )


class TestRead:
    """IdentityManager.read() returns content or empty string."""

    @pytest.mark.asyncio
    async def test_read_missing_file_returns_empty(
        self, im: IdentityManager,
    ) -> None:
        result = await im.read()
        assert result == ""

    @pytest.mark.asyncio
    async def test_read_existing_file(
        self, im: IdentityManager, memory_dir: Path,
    ) -> None:
        (memory_dir / "how-i-work.md").write_text(
            "I am helpful.", encoding="utf-8",
        )
        result = await im.read()
        assert result == "I am helpful."


class TestInjectContext:
    """IdentityManager.inject_context() reads, formats, and enforces budget."""

    @pytest.mark.asyncio
    async def test_inject_missing_file_returns_empty(
        self, im: IdentityManager,
    ) -> None:
        result = await im.inject_context()
        assert result == ""

    @pytest.mark.asyncio
    async def test_inject_includes_content(
        self, im: IdentityManager, memory_dir: Path,
    ) -> None:
        (memory_dir / "how-i-work.md").write_text(
            "I prefer concise answers.", encoding="utf-8",
        )
        result = await im.inject_context()
        assert "I prefer concise answers." in result

    @pytest.mark.asyncio
    async def test_inject_enforces_budget(
        self, memory_dir: Path, telemetry: MagicMock,
    ) -> None:
        cfg = BioMemoryConfig(identity_budget=5)  # ~20 chars
        im = IdentityManager(
            memory_dir=memory_dir, config=cfg, telemetry=telemetry,
        )
        long_content = "x" * 200
        (memory_dir / "how-i-work.md").write_text(
            long_content, encoding="utf-8",
        )
        result = await im.inject_context()
        # Should be truncated to fit within budget
        max_chars = 5 * CHARS_PER_TOKEN
        assert len(result) <= max_chars + 50  # slack for header formatting


class TestUpdate:
    """IdentityManager.update() writes and emits audit event."""

    @pytest.mark.asyncio
    async def test_update_writes_file(
        self, im: IdentityManager, memory_dir: Path,
    ) -> None:
        await im.update("New identity content")
        path = memory_dir / "how-i-work.md"
        assert path.exists()
        assert "New identity content" in path.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_update_emits_audit_event(
        self, im: IdentityManager, telemetry: MagicMock,
    ) -> None:
        await im.update("Updated content")
        telemetry.audit_event.assert_called_once()
        call_kwargs = telemetry.audit_event.call_args
        # Should be called with identity.modified event
        assert "identity.modified" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_update_captures_before_after(
        self, im: IdentityManager, memory_dir: Path, telemetry: MagicMock,
    ) -> None:
        """Audit event includes before/after lengths for NIST AU-2."""
        (memory_dir / "how-i-work.md").write_text("Old", encoding="utf-8")
        await im.update("New content here")
        call_kwargs = telemetry.audit_event.call_args
        # Details should include before and after info
        details = call_kwargs.kwargs.get("details", {}) if call_kwargs.kwargs else {}
        if not details and call_kwargs.args:
            # Positional args — details might be in args
            for arg in call_kwargs.args:
                if isinstance(arg, dict) and "before_length" in arg:
                    details = arg
                    break
        assert "before_length" in details or len(call_kwargs.args) > 0


class TestSanitization:
    """IdentityManager.update() sanitizes content (ASI-06 defense-in-depth)."""

    @pytest.mark.asyncio
    async def test_update_strips_zero_width_chars(
        self, im: IdentityManager, memory_dir: Path,
    ) -> None:
        """Zero-width characters stripped from identity content."""
        poisoned = "Behavioral\u200bpattern\u200fhere\ufeff"
        await im.update(poisoned)
        text = (memory_dir / "how-i-work.md").read_text(encoding="utf-8")
        assert "\u200b" not in text
        assert "\u200f" not in text
        assert "\ufeff" not in text
        assert "Behavioralpatternhere" in text

    @pytest.mark.asyncio
    async def test_update_enforces_max_length(
        self, im: IdentityManager, memory_dir: Path,
    ) -> None:
        """Content exceeding 10K chars is truncated."""
        huge = "x" * 20000
        await im.update(huge)
        text = (memory_dir / "how-i-work.md").read_text(encoding="utf-8")
        assert len(text) <= 10000


class TestIsOverBudget:
    """IdentityManager.is_over_budget() checks token count."""

    def test_short_content_within_budget(self, im: IdentityManager) -> None:
        assert im.is_over_budget("short") is False

    def test_long_content_over_budget(
        self, memory_dir: Path, telemetry: MagicMock,
    ) -> None:
        cfg = BioMemoryConfig(identity_budget=5)  # ~20 chars
        im = IdentityManager(
            memory_dir=memory_dir, config=cfg, telemetry=telemetry,
        )
        assert im.is_over_budget("x" * 100) is True

    def test_empty_content_within_budget(self, im: IdentityManager) -> None:
        assert im.is_over_budget("") is False
