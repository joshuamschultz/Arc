"""Regression tests for validate-then-open filesystem races."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.builtins.capabilities import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


async def test_read_rejects_symlink_swapped_after_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcagent.builtins.capabilities import read as read_module
    from arcagent.tools._secure_workspace_file import read_regular_file as secure_read

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("inside")
    outside.write_text("secret outside")
    _runtime.configure(workspace=workspace)

    def race(path: Path, roots: tuple[Path, ...], *, max_bytes: int) -> Any:
        path.unlink()
        path.symlink_to(outside)
        return secure_read(path, roots, max_bytes=max_bytes)

    monkeypatch.setattr(read_module, "read_regular_file", race)
    result = await read_module.read("note.txt")

    assert "secret outside" not in result
    assert result.startswith("Error:")


async def test_write_rejects_parent_swapped_to_symlink_after_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcagent.builtins.capabilities import write as write_module
    from arcagent.tools._secure_workspace_file import atomic_write_regular_file as secure_write

    workspace = tmp_path / "workspace"
    parent = workspace / "dir"
    outside = tmp_path / "outside"
    parent.mkdir(parents=True)
    outside.mkdir()
    _runtime.configure(workspace=workspace)

    def race(path: Path, roots: tuple[Path, ...], data: bytes) -> None:
        parent.rmdir()
        parent.symlink_to(outside)
        secure_write(path, roots, data)

    monkeypatch.setattr(write_module, "atomic_write_regular_file", race)
    result = await write_module.write("dir/new.txt", "payload")

    assert result.startswith("Error:")
    assert not (outside / "new.txt").exists()


async def test_edit_detects_target_rename_between_read_and_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcagent.builtins.capabilities import edit as edit_module
    from arcagent.tools._secure_workspace_file import atomic_write_regular_file as secure_write

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("old value")
    _runtime.configure(workspace=workspace)

    def race(
        path: Path,
        roots: tuple[Path, ...],
        data: bytes,
        *,
        expected: Any = None,
    ) -> None:
        path.replace(workspace / "moved.txt")
        path.write_text("replacement owned by another writer")
        secure_write(path, roots, data, expected=expected)

    monkeypatch.setattr(edit_module, "atomic_write_regular_file", race)
    result = await edit_module.edit("note.txt", "old", "new")

    assert result.startswith("Error: File changed while editing")
    assert target.read_text() == "replacement owned by another writer"
    assert (workspace / "moved.txt").read_text() == "old value"
