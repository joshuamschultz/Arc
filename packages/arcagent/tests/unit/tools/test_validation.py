"""Tests for workspace path validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.core.errors import ToolError
from arcagent.tools._validation import resolve_workspace_path


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "ws"


@pytest.fixture(autouse=True)
def _create_workspace(workspace: Path) -> None:
    workspace.mkdir()


class TestResolveWorkspacePath:
    """Core path resolution and security boundary tests."""

    def test_relative_path_resolves_inside_workspace(self, workspace: Path) -> None:
        result = resolve_workspace_path("foo.txt", workspace)
        assert result == workspace / "foo.txt"

    def test_relative_nested_path(self, workspace: Path) -> None:
        result = resolve_workspace_path("sub/dir/file.py", workspace)
        assert result == workspace / "sub" / "dir" / "file.py"

    def test_absolute_path_inside_workspace(self, workspace: Path) -> None:
        target = workspace / "inside.txt"
        result = resolve_workspace_path(str(target), workspace)
        assert result == target

    def test_absolute_path_outside_workspace_raises(self, workspace: Path) -> None:
        with pytest.raises(ToolError) as exc_info:
            resolve_workspace_path("/etc/passwd", workspace)
        assert exc_info.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"

    def test_traversal_attack_blocked(self, workspace: Path) -> None:
        with pytest.raises(ToolError) as exc_info:
            resolve_workspace_path("../../etc/passwd", workspace)
        assert exc_info.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"

    def test_null_byte_injection_blocked(self, workspace: Path) -> None:
        with pytest.raises(ToolError) as exc_info:
            resolve_workspace_path("file\x00.txt", workspace)
        assert exc_info.value.code == "TOOL_INVALID_PATH"

    def test_workspace_itself_is_valid(self, workspace: Path) -> None:
        """Workspace root path should resolve to itself."""
        result = resolve_workspace_path(".", workspace)
        assert result == workspace.resolve()

    def test_dot_dot_within_workspace_ok(self, workspace: Path) -> None:
        """sub/../file.txt should resolve to file.txt inside workspace."""
        result = resolve_workspace_path("sub/../file.txt", workspace)
        assert result == workspace / "file.txt"

    def test_symlink_rejected_by_default(self, workspace: Path) -> None:
        target = workspace / "real.txt"
        target.write_text("content")
        link = workspace / "link.txt"
        link.symlink_to(target)
        with pytest.raises(ToolError) as exc_info:
            resolve_workspace_path("link.txt", workspace)
        assert exc_info.value.code == "TOOL_SYMLINK_DENIED"

    def test_symlink_allowed_when_flag_set(self, workspace: Path) -> None:
        target = workspace / "real.txt"
        target.write_text("content")
        link = workspace / "link.txt"
        link.symlink_to(target)
        result = resolve_workspace_path("link.txt", workspace, allow_symlinks=True)
        assert result == target.resolve()


class TestSymlinkPathWalk:
    """Tests for symlink detection in intermediate path components."""

    def test_symlink_directory_in_path_rejected(self, workspace: Path) -> None:
        """Symlink directory as intermediate component is detected."""
        real_dir = workspace / "real_sub"
        real_dir.mkdir()
        (real_dir / "file.txt").write_text("content")
        link_dir = workspace / "link_sub"
        link_dir.symlink_to(real_dir)
        with pytest.raises(ToolError) as exc_info:
            resolve_workspace_path("link_sub/file.txt", workspace)
        assert exc_info.value.code == "TOOL_SYMLINK_DENIED"

    def test_deeply_nested_symlink_detected(self, workspace: Path) -> None:
        """Symlink several levels deep is still detected."""
        deep = workspace / "a" / "b"
        deep.mkdir(parents=True)
        real = workspace / "a" / "b" / "real"
        real.mkdir()
        (real / "data.txt").write_text("data")
        link = workspace / "a" / "b" / "link"
        link.symlink_to(real)
        with pytest.raises(ToolError) as exc_info:
            resolve_workspace_path("a/b/link/data.txt", workspace)
        assert exc_info.value.code == "TOOL_SYMLINK_DENIED"


class TestAllowedPaths:
    """Tests for allowed_paths parameter (tool scope expansion)."""

    def test_path_outside_workspace_allowed_when_in_allowed_paths(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        extra = tmp_path / "shared"
        extra.mkdir()
        target = extra / "data.txt"
        target.write_text("shared data")
        result = resolve_workspace_path(str(target), workspace, allowed_paths=[extra])
        assert result == target

    def test_path_outside_workspace_and_allowed_paths_raises(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        extra = tmp_path / "shared"
        extra.mkdir()
        with pytest.raises(ToolError) as exc_info:
            resolve_workspace_path("/etc/passwd", workspace, allowed_paths=[extra])
        assert exc_info.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"

    def test_empty_allowed_paths_behaves_like_none(self, workspace: Path) -> None:
        with pytest.raises(ToolError) as exc_info:
            resolve_workspace_path("/etc/passwd", workspace, allowed_paths=[])
        assert exc_info.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"

    def test_workspace_path_still_works_with_allowed_paths(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        extra = tmp_path / "shared"
        extra.mkdir()
        result = resolve_workspace_path("foo.txt", workspace, allowed_paths=[extra])
        assert result == workspace / "foo.txt"
