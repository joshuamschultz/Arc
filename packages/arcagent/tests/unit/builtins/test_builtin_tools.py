"""SPEC-021 Task 2.1 — built-in file/exec tools (decorator form).

The 7 ported tools (read/write/edit/bash/grep/find/ls) live under
:mod:`arcagent.builtins.capabilities`. Each is a top-level async
function stamped via ``@tool``. They read workspace context from
:mod:`arcagent.builtins.capabilities._runtime`.

This test exercises the happy path for each tool plus a representative
failure mode. Behavior must match the legacy ``arcagent.tools.*``
implementations they replace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.builtins.capabilities import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    """Each test gets a clean runtime (no leak between tests)."""
    _runtime.reset()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    _runtime.configure(workspace=tmp_path)
    return tmp_path


@pytest.mark.asyncio
class TestReadTool:
    async def test_read_file(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.read import read

        (workspace / "hello.txt").write_text("line one\nline two\n")
        result = await read(file_path="hello.txt")
        assert "1\tline one" in result
        assert "2\tline two" in result

    async def test_read_missing_returns_error(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.read import read

        result = await read(file_path="nope.txt")
        assert result.startswith("Error: File not found")

    async def test_read_offset_limit(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.read import read

        (workspace / "f.txt").write_text("a\nb\nc\nd\n")
        result = await read(file_path="f.txt", offset=2, limit=2)
        assert "2\tb" in result
        assert "3\tc" in result
        assert "1\ta" not in result


@pytest.mark.asyncio
class TestWriteTool:
    async def test_write_creates_file(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.write import write

        result = await write(file_path="out.txt", content="hello")
        assert "Written 5 bytes" in result
        assert (workspace / "out.txt").read_text() == "hello"

    async def test_write_creates_parents(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.write import write

        await write(file_path="dir/sub/out.txt", content="x")
        assert (workspace / "dir" / "sub" / "out.txt").exists()


@pytest.mark.asyncio
class TestEditTool:
    async def test_edit_unique_replacement(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.edit import edit

        (workspace / "f.txt").write_text("foo bar baz")
        result = await edit(file_path="f.txt", old_string="bar", new_string="qux")
        assert "Replaced 1 occurrence" in result
        assert (workspace / "f.txt").read_text() == "foo qux baz"

    async def test_edit_ambiguous_rejected(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.edit import edit

        (workspace / "f.txt").write_text("foo foo foo")
        result = await edit(file_path="f.txt", old_string="foo", new_string="bar")
        assert "found 3 times" in result

    async def test_edit_replace_all(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.edit import edit

        (workspace / "f.txt").write_text("foo foo foo")
        result = await edit(
            file_path="f.txt",
            old_string="foo",
            new_string="bar",
            replace_all=True,
        )
        assert "Replaced 3 occurrence" in result


@pytest.mark.asyncio
class TestBashTool:
    async def test_bash_echoes(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.bash import bash

        result = await bash(command="echo hello")
        assert "hello" in result

    async def test_bash_nonzero_exit(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.bash import bash

        result = await bash(command="false")
        assert "Exit code: 1" in result


@pytest.mark.asyncio
class TestGrepTool:
    async def test_grep_finds_matches(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.grep import grep

        (workspace / "a.py").write_text("def hello():\n    pass\n")
        (workspace / "b.py").write_text("class X:\n    def hello(self):\n        pass\n")
        result = await grep(pattern="hello")
        assert "a.py" in result
        assert "b.py" in result

    async def test_grep_no_matches(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.grep import grep

        result = await grep(pattern="nothing")
        assert result == "No matches found."


@pytest.mark.asyncio
class TestFindTool:
    async def test_find_matches_glob(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.find import find

        (workspace / "a.py").write_text("")
        (workspace / "b.txt").write_text("")
        result = await find(pattern="**/*.py")
        assert "a.py" in result
        assert "b.txt" not in result

    async def test_find_rejects_traversal(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.find import find

        result = await find(pattern="../*.py")
        assert "must not contain" in result


@pytest.mark.asyncio
class TestLsTool:
    async def test_ls_shows_dirs_and_files(self, workspace: Path) -> None:
        from arcagent.builtins.capabilities.ls import ls

        (workspace / "subdir").mkdir()
        (workspace / "f.txt").write_text("x")
        result = await ls()
        assert " d  subdir/" in result
        assert " f  f.txt" in result


@pytest.mark.asyncio
class TestRuntimeContract:
    async def test_unconfigured_raises(self) -> None:
        from arcagent.builtins.capabilities.read import read

        # _reset_runtime fixture ran; configure was never called.
        with pytest.raises(RuntimeError, match="before runtime is configured"):
            await read(file_path="anything.txt")
