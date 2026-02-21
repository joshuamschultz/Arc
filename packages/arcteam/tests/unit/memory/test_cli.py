"""Tests for memory CLI commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcteam.memory.cli import build_memory_parser, run_memory_command


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


class TestMemoryCLIParser:
    """Parser should accept memory subcommands."""

    def test_status_command(self) -> None:
        parser = build_memory_parser()
        args = parser.parse_args(["status"])
        assert args.command == "status"

    def test_search_command(self) -> None:
        parser = build_memory_parser()
        args = parser.parse_args(["search", "nuclear physicist"])
        assert args.command == "search"
        assert args.query == "nuclear physicist"

    def test_entity_show_command(self) -> None:
        parser = build_memory_parser()
        args = parser.parse_args(["entity", "show", "alice"])
        assert args.entity_command == "show"
        assert args.entity_id == "alice"

    def test_entity_list_command(self) -> None:
        parser = build_memory_parser()
        args = parser.parse_args(["entity", "list"])
        assert args.entity_command == "list"

    def test_index_rebuild_command(self) -> None:
        parser = build_memory_parser()
        args = parser.parse_args(["index", "rebuild"])
        assert args.index_command == "rebuild"

    def test_promote_command(self) -> None:
        parser = build_memory_parser()
        args = parser.parse_args([
            "promote", "alice",
            "--name", "Alice",
            "--type", "person",
            "--content", "# Alice",
        ])
        assert args.command == "promote"
        assert args.entity_id == "alice"


class TestMemoryCLIExecution:
    """CLI commands should execute against TeamMemoryService."""

    @pytest.mark.asyncio
    async def test_status_runs(self, root: Path) -> None:
        parser = build_memory_parser()
        args = parser.parse_args(["--root", str(root), "status"])
        result = await run_memory_command(args)
        assert result == 0

    @pytest.mark.asyncio
    async def test_search_empty(self, root: Path) -> None:
        parser = build_memory_parser()
        args = parser.parse_args(["--root", str(root), "search", "anything"])
        result = await run_memory_command(args)
        assert result == 0

    @pytest.mark.asyncio
    async def test_index_rebuild(self, root: Path) -> None:
        parser = build_memory_parser()
        args = parser.parse_args(["--root", str(root), "index", "rebuild"])
        result = await run_memory_command(args)
        assert result == 0
