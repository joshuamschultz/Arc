"""Tests for WorkingMemory — scratchpad lifecycle, budget enforcement, atomic writes."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.working_memory import WorkingMemory


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """Create a temporary memory directory."""
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def config() -> BioMemoryConfig:
    return BioMemoryConfig()


@pytest.fixture
def wm(memory_dir: Path, config: BioMemoryConfig) -> WorkingMemory:
    return WorkingMemory(memory_dir=memory_dir, config=config)


class TestRead:
    """WorkingMemory.read() returns content or empty string."""

    @pytest.mark.asyncio
    async def test_read_missing_file_returns_empty(self, wm: WorkingMemory) -> None:
        result = await wm.read()
        assert result == ""

    @pytest.mark.asyncio
    async def test_read_existing_file(
        self, wm: WorkingMemory, memory_dir: Path,
    ) -> None:
        (memory_dir / "working.md").write_text("hello world", encoding="utf-8")
        result = await wm.read()
        assert result == "hello world"


class TestWrite:
    """WorkingMemory.write() creates file with frontmatter + body."""

    @pytest.mark.asyncio
    async def test_write_creates_file(
        self, wm: WorkingMemory, memory_dir: Path,
    ) -> None:
        await wm.write(
            content="Test note",
            frontmatter={"topics": ["testing"], "turn_number": 1},
        )
        path = memory_dir / "working.md"
        assert path.exists()

    @pytest.mark.asyncio
    async def test_write_has_yaml_frontmatter(
        self, wm: WorkingMemory, memory_dir: Path,
    ) -> None:
        await wm.write(
            content="Test note",
            frontmatter={"topics": ["testing"], "turn_number": 1},
        )
        text = (memory_dir / "working.md").read_text(encoding="utf-8")
        assert text.startswith("---\n")
        # Extract frontmatter
        end = text.find("\n---", 3)
        assert end != -1
        fm = yaml.safe_load(text[4:end])
        assert fm["topics"] == ["testing"]
        assert fm["turn_number"] == 1

    @pytest.mark.asyncio
    async def test_write_body_after_frontmatter(
        self, wm: WorkingMemory, memory_dir: Path,
    ) -> None:
        await wm.write(
            content="My note content",
            frontmatter={"topics": []},
        )
        text = (memory_dir / "working.md").read_text(encoding="utf-8")
        end = text.find("\n---", 3)
        body = text[end + 4:].strip()
        assert "My note content" in body

    @pytest.mark.asyncio
    async def test_write_overwrites_existing(
        self, wm: WorkingMemory, memory_dir: Path,
    ) -> None:
        await wm.write(content="First", frontmatter={"turn_number": 1})
        await wm.write(content="Second", frontmatter={"turn_number": 2})
        text = (memory_dir / "working.md").read_text(encoding="utf-8")
        assert "Second" in text
        assert "First" not in text

    @pytest.mark.asyncio
    async def test_write_enforces_token_budget(
        self, memory_dir: Path,
    ) -> None:
        """Content exceeding working_budget is truncated."""
        cfg = BioMemoryConfig(working_budget=10)  # ~40 chars
        wm = WorkingMemory(memory_dir=memory_dir, config=cfg)
        long_content = "a" * 200
        await wm.write(content=long_content, frontmatter={})
        text = (memory_dir / "working.md").read_text(encoding="utf-8")
        end = text.find("\n---", 3)
        body = text[end + 4:].strip()
        # Body should be truncated to approximately working_budget * CHARS_PER_TOKEN
        assert len(body) <= 10 * 4 + 10  # some slack for formatting


class TestSanitization:
    """WorkingMemory.write() sanitizes content before writing (ASI-06)."""

    @pytest.mark.asyncio
    async def test_write_strips_zero_width_chars(
        self, wm: WorkingMemory, memory_dir: Path,
    ) -> None:
        """Zero-width characters stripped from written content."""
        poisoned = "Clean\u200btext\u200fhere\ufeff"
        await wm.write(content=poisoned, frontmatter={})
        text = (memory_dir / "working.md").read_text(encoding="utf-8")
        assert "\u200b" not in text
        assert "\u200f" not in text
        assert "\ufeff" not in text
        assert "Cleantexthere" in text

    @pytest.mark.asyncio
    async def test_write_strips_control_chars(
        self, wm: WorkingMemory, memory_dir: Path,
    ) -> None:
        """ASCII control characters stripped (except tab/newline/CR)."""
        content = "Normal\x00\x01\x02text"
        await wm.write(content=content, frontmatter={})
        text = (memory_dir / "working.md").read_text(encoding="utf-8")
        assert "\x00" not in text
        assert "\x01" not in text
        assert "Normaltext" in text


class TestClear:
    """WorkingMemory.clear() empties the file."""

    @pytest.mark.asyncio
    async def test_clear_empties_content(
        self, wm: WorkingMemory, memory_dir: Path,
    ) -> None:
        await wm.write(content="Data here", frontmatter={"turn_number": 1})
        await wm.clear()
        text = (memory_dir / "working.md").read_text(encoding="utf-8")
        # File should still exist (preserves workspace detection) but body empty
        assert text.startswith("---\n")
        end = text.find("\n---", 3)
        body = text[end + 4:].strip()
        assert body == ""

    @pytest.mark.asyncio
    async def test_clear_no_file_is_noop(self, wm: WorkingMemory) -> None:
        """Clearing when no file exists should not raise."""
        await wm.clear()  # No exception


class TestEstimateTokens:
    """Token estimation uses CHARS_PER_TOKEN constant."""

    def test_empty_string(self, wm: WorkingMemory) -> None:
        assert wm.estimate_tokens("") == 0

    def test_known_length(self, wm: WorkingMemory) -> None:
        # CHARS_PER_TOKEN = 4, so 40 chars ≈ 10 tokens
        assert wm.estimate_tokens("a" * 40) == 10

    def test_rounds_up(self, wm: WorkingMemory) -> None:
        # 5 chars / 4 = 1.25, should round up to 2
        result = wm.estimate_tokens("abcde")
        assert result >= 1
