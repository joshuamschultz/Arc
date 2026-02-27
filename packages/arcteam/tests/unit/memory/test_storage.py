"""Tests for MemoryStorage (YAML frontmatter + markdown I/O)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcteam.memory.storage import MemoryStorage
from arcteam.memory.types import EntityMetadata, IndexEntry


def _make_metadata(**overrides: object) -> EntityMetadata:
    defaults = {
        "entity_type": "person",
        "entity_id": "john-doe",
        "name": "John Doe",
        "last_updated": "2026-02-21",
    }
    defaults.update(overrides)
    return EntityMetadata(**defaults)  # type: ignore[arg-type]


def _make_index(
    entity_id: str = "john-doe", path: str = "person/john-doe.md"
) -> dict[str, IndexEntry]:
    return {entity_id: IndexEntry(entity_id=entity_id, path=path, entity_type="person")}


class TestMemoryStorageWrite:
    """write_entity should create markdown files with YAML frontmatter."""

    @pytest.mark.asyncio
    async def test_write_creates_file(self, tmp_path: Path) -> None:
        storage = MemoryStorage(tmp_path)
        meta = _make_metadata()
        path = await storage.write_entity("john-doe", meta, "# John Doe\n\nA person.")
        assert path.exists()
        assert path.suffix == ".md"

    @pytest.mark.asyncio
    async def test_write_creates_type_directory(self, tmp_path: Path) -> None:
        storage = MemoryStorage(tmp_path)
        meta = _make_metadata(entity_type="organization", entity_id="nnsa")
        path = await storage.write_entity("nnsa", meta, "# NNSA")
        assert path.parent.name == "organization"

    @pytest.mark.asyncio
    async def test_write_content_has_frontmatter(self, tmp_path: Path) -> None:
        storage = MemoryStorage(tmp_path)
        meta = _make_metadata(tags=["federal"])
        await storage.write_entity("john-doe", meta, "# John Doe")
        content = (tmp_path / "person" / "john-doe.md").read_text()
        assert content.startswith("---\n")
        assert "entity_id: john-doe" in content
        assert "# John Doe" in content


class TestMemoryStorageRead:
    """read_entity should parse frontmatter + body from markdown files."""

    @pytest.mark.asyncio
    async def test_read_roundtrip(self, tmp_path: Path) -> None:
        storage = MemoryStorage(tmp_path)
        meta = _make_metadata(links_to=["DOE", "genesis-mission"])
        await storage.write_entity("john-doe", meta, "# John Doe\n\nSome content.")
        index = _make_index()
        result = await storage.read_entity("john-doe", index)
        assert result is not None
        assert result.metadata.entity_id == "john-doe"
        assert result.metadata.links_to == ["DOE", "genesis-mission"]
        assert "Some content." in result.content

    @pytest.mark.asyncio
    async def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        storage = MemoryStorage(tmp_path)
        index = _make_index()
        result = await storage.read_entity("nonexistent", index)
        assert result is None

    @pytest.mark.asyncio
    async def test_read_not_in_index_returns_none(self, tmp_path: Path) -> None:
        storage = MemoryStorage(tmp_path)
        result = await storage.read_entity("john-doe", {})
        assert result is None


class TestMemoryStorageDelete:
    """delete_entity should remove the file."""

    @pytest.mark.asyncio
    async def test_delete_existing(self, tmp_path: Path) -> None:
        storage = MemoryStorage(tmp_path)
        meta = _make_metadata()
        await storage.write_entity("john-doe", meta, "# John Doe")
        index = _make_index()
        deleted = await storage.delete_entity("john-doe", index)
        assert deleted is True
        assert not (tmp_path / "person" / "john-doe.md").exists()

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, tmp_path: Path) -> None:
        storage = MemoryStorage(tmp_path)
        index = _make_index()
        deleted = await storage.delete_entity("nonexistent", index)
        assert deleted is False


class TestMemoryStorageFrontmatter:
    """read_frontmatter_only should parse metadata without reading body."""

    @pytest.mark.asyncio
    async def test_frontmatter_only(self, tmp_path: Path) -> None:
        storage = MemoryStorage(tmp_path)
        meta = _make_metadata(tags=["test", "federal"])
        await storage.write_entity("john-doe", meta, "# Lots of content\n\n" * 100)
        path = tmp_path / "person" / "john-doe.md"
        result = await storage.read_frontmatter_only(path)
        assert result is not None
        assert result.entity_id == "john-doe"
        assert result.tags == ["test", "federal"]


class TestMemoryStorageListFiles:
    """list_entity_files should discover all .md files."""

    @pytest.mark.asyncio
    async def test_list_files(self, tmp_path: Path) -> None:
        storage = MemoryStorage(tmp_path)
        await storage.write_entity("a", _make_metadata(entity_id="a"), "# A")
        await storage.write_entity(
            "b",
            _make_metadata(entity_type="organization", entity_id="b", name="B"),
            "# B",
        )
        files = await storage.list_entity_files()
        assert len(files) == 2
        names = {f.stem for f in files}
        assert names == {"a", "b"}

    @pytest.mark.asyncio
    async def test_list_empty(self, tmp_path: Path) -> None:
        storage = MemoryStorage(tmp_path)
        files = await storage.list_entity_files()
        assert files == []


class TestTokenEstimation:
    """estimate_tokens should approximate token count."""

    def test_estimate(self) -> None:
        storage = MemoryStorage(Path("/tmp"))
        # 10 words * 1.3 = 13 tokens
        assert storage.estimate_tokens("one two three four five six seven eight nine ten") == 13
