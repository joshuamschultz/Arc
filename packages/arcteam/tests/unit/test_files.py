"""Tests for arcteam.files.TeamFileStore — path-traversal containment."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcteam.files import TeamFileStore


@pytest.fixture
def team_root(tmp_path: Path) -> Path:
    return tmp_path / "team"


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    src = tmp_path / "report.pdf"
    src.write_text("payload")
    return src


class TestStoreContainment:
    """store() must reject any name that could escape the team root."""

    async def test_valid_name_stores(self, team_root: Path, source_file: Path) -> None:
        store = TeamFileStore(team_root)
        result = await store.store(source_file, "builder")
        assert Path(result["path"]).is_relative_to(team_root)

    async def test_dotted_name_allowed(self, team_root: Path, source_file: Path) -> None:
        store = TeamFileStore(team_root)
        result = await store.store(source_file, "brad.agent")
        assert Path(result["path"]).is_relative_to(team_root)

    @pytest.mark.parametrize(
        "evil_name",
        ["../evil", "..", ".", "../../etc", "a/b", "/abs", ".hidden"],
    )
    async def test_traversal_name_rejected(
        self, team_root: Path, source_file: Path, evil_name: str
    ) -> None:
        store = TeamFileStore(team_root)
        with pytest.raises(ValueError, match="Unsafe agent name"):
            await store.store(source_file, evil_name)

    async def test_traversal_creates_no_directory_outside_root(
        self, team_root: Path, source_file: Path
    ) -> None:
        """Validation must precede mkdir — a rejected name creates nothing."""
        store = TeamFileStore(team_root)
        with pytest.raises(ValueError):
            await store.store(source_file, "../escaped")
        assert not (team_root.parent / "escaped").exists()


class TestListFilesContainment:
    """list_files() must apply the same containment as store()."""

    @pytest.mark.parametrize("evil_name", ["../evil", "..", "a/b"])
    async def test_list_traversal_rejected(self, team_root: Path, evil_name: str) -> None:
        store = TeamFileStore(team_root)
        with pytest.raises(ValueError, match="Unsafe agent name"):
            await store.list_files(evil_name)

    async def test_list_valid_agent(self, team_root: Path, source_file: Path) -> None:
        store = TeamFileStore(team_root)
        await store.store(source_file, "builder")
        listed = await store.list_files("builder")
        assert [f["filename"] for f in listed] == ["report.pdf"]
