"""Tests for DailyNotes — append-only daily journal."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.daily_notes import DailyNotes


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def config() -> BioMemoryConfig:
    return BioMemoryConfig()


@pytest.fixture
def daily_notes(memory_dir: Path, config: BioMemoryConfig) -> DailyNotes:
    return DailyNotes(memory_dir=memory_dir, config=config)


class TestAppend:
    """DailyNotes.append() creates and appends to daily files."""

    @pytest.mark.asyncio
    async def test_creates_file_on_first_append(
        self, daily_notes: DailyNotes,
    ) -> None:
        path = await daily_notes.append(["First entry"], agent_id="test-agent")
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "First entry" in text

    @pytest.mark.asyncio
    async def test_file_has_frontmatter(
        self, daily_notes: DailyNotes,
    ) -> None:
        path = await daily_notes.append(["Entry"])
        text = path.read_text(encoding="utf-8")
        end = text.find("\n---", 3)
        fm = yaml.safe_load(text[4:end])
        assert "date" in fm

    @pytest.mark.asyncio
    async def test_appends_to_existing_file(
        self, daily_notes: DailyNotes,
    ) -> None:
        await daily_notes.append(["First entry"])
        path = await daily_notes.append(["Second entry"])
        text = path.read_text(encoding="utf-8")
        assert "First entry" in text
        assert "Second entry" in text

    @pytest.mark.asyncio
    async def test_multiple_entries_in_single_append(
        self, daily_notes: DailyNotes,
    ) -> None:
        path = await daily_notes.append(["Entry A", "Entry B", "Entry C"])
        text = path.read_text(encoding="utf-8")
        assert "Entry A" in text
        assert "Entry B" in text
        assert "Entry C" in text

    @pytest.mark.asyncio
    async def test_creates_directory_if_missing(
        self, daily_notes: DailyNotes,
    ) -> None:
        assert not daily_notes.directory.exists()
        await daily_notes.append(["Entry"])
        assert daily_notes.directory.exists()

    @pytest.mark.asyncio
    async def test_entries_are_sanitized(
        self, daily_notes: DailyNotes,
    ) -> None:
        """Entries over max_length are truncated."""
        long_entry = "x" * 2000
        path = await daily_notes.append([long_entry])
        text = path.read_text(encoding="utf-8")
        # sanitize_text truncates at 1000
        assert len(text) < 2000


class TestReadDate:
    """DailyNotes.read_date() retrieves specific dates."""

    @pytest.mark.asyncio
    async def test_read_nonexistent_date(
        self, daily_notes: DailyNotes,
    ) -> None:
        result = await daily_notes.read_date("2020-01-01")
        assert result == ""

    @pytest.mark.asyncio
    async def test_read_existing_date(
        self, daily_notes: DailyNotes,
    ) -> None:
        path = await daily_notes.append(["Test entry"])
        date_str = path.stem  # YYYY-MM-DD
        result = await daily_notes.read_date(date_str)
        assert "Test entry" in result


class TestDirectory:
    """DailyNotes.directory property returns the correct path."""

    def test_directory_matches_config(
        self, daily_notes: DailyNotes, memory_dir: Path,
    ) -> None:
        assert daily_notes.directory == memory_dir / "daily-notes"
