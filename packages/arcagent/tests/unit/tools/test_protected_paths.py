"""SPEC-035 sub-scope A — goal-lock / protected-path denylist.

The agent's mutating tools (write/edit/bash) must refuse to modify a
protected path (identity.md/policy.md/context.md + operator config), at
every tier, and audit the denial. Reads of the same file still succeed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.builtins.capabilities import _runtime
from arcagent.core.errors import ToolError
from arcagent.tools._validation import (
    DEFAULT_PROTECTED_NAMES,
    is_protected_path,
    resolve_protected_paths,
)


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


class _RecordingSink:
    """Collects audit events for assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


class TestIsProtectedPath:
    def test_default_names_are_protected(self, tmp_path: Path) -> None:
        protected = resolve_protected_paths(tmp_path, [])
        for name in DEFAULT_PROTECTED_NAMES:
            assert is_protected_path((tmp_path / name).resolve(), protected)

    def test_normal_file_not_protected(self, tmp_path: Path) -> None:
        protected = resolve_protected_paths(tmp_path, [])
        assert not is_protected_path((tmp_path / "notes.txt").resolve(), protected)

    def test_operator_config_extends_set(self, tmp_path: Path) -> None:
        protected = resolve_protected_paths(tmp_path, ["secret.md"])
        assert is_protected_path((tmp_path / "secret.md").resolve(), protected)

    def test_hardlink_to_protected_is_protected_by_inode(self, tmp_path: Path) -> None:
        # A hardlink is the SAME inode as identity.md under a different name;
        # string-equality of resolved paths misses it, inode identity catches it.
        (tmp_path / "identity.md").write_text("goal\n")
        link = tmp_path / "hardlink.md"
        link.hardlink_to(tmp_path / "identity.md")
        protected = resolve_protected_paths(tmp_path, [])
        assert is_protected_path(link.resolve(), protected)

    def test_symlink_to_protected_is_protected_by_inode(self, tmp_path: Path) -> None:
        (tmp_path / "identity.md").write_text("goal\n")
        link = tmp_path / "link.md"
        link.symlink_to(tmp_path / "identity.md")
        protected = resolve_protected_paths(tmp_path, [])
        assert is_protected_path(link.resolve(), protected)

    def test_case_variant_of_uncreated_protected_is_protected(self, tmp_path: Path) -> None:
        # identity.md does NOT exist yet; a to-be-created IDENTITY.md would BE it
        # on a case-insensitive filesystem, so a case-normalized fallback denies.
        protected = resolve_protected_paths(tmp_path, [])
        assert is_protected_path((tmp_path / "IDENTITY.md"), protected)


@pytest.mark.asyncio
class TestWriteEditGuard:
    async def test_write_to_identity_denied_and_audited(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.write import write

        (tmp_path / "identity.md").write_text("operator goal\n")
        sink = _RecordingSink()
        _runtime.configure(
            workspace=tmp_path,
            protected_paths=resolve_protected_paths(tmp_path, []),
            audit_sink=sink,
        )
        with pytest.raises(ToolError) as exc:
            await write(file_path="identity.md", content="hijacked")
        assert exc.value.code == "TOOL_PROTECTED_PATH"
        # File is unchanged.
        assert (tmp_path / "identity.md").read_text() == "operator goal\n"
        # Audit emitted with tool, actor, path.
        denied = [p for e, p in sink.events if e == "tool.protected_path.denied"]
        assert denied and denied[0]["tool"] == "write"
        assert denied[0]["path"].endswith("identity.md")
        assert "actor_did" in denied[0]

    async def test_edit_to_policy_denied(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.edit import edit

        (tmp_path / "policy.md").write_text("allow: read\n")
        _runtime.configure(
            workspace=tmp_path,
            protected_paths=resolve_protected_paths(tmp_path, []),
        )
        with pytest.raises(ToolError) as exc:
            await edit(file_path="policy.md", old_string="read", new_string="all")
        assert exc.value.code == "TOOL_PROTECTED_PATH"
        assert (tmp_path / "policy.md").read_text() == "allow: read\n"

    async def test_write_normal_file_still_works(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.write import write

        _runtime.configure(
            workspace=tmp_path,
            protected_paths=resolve_protected_paths(tmp_path, []),
        )
        result = await write(file_path="notes.txt", content="hi")
        assert "Written" in result
        assert (tmp_path / "notes.txt").read_text() == "hi"

    async def test_write_to_case_variant_of_identity_denied(self, tmp_path: Path) -> None:
        # On a case-insensitive filesystem, IDENTITY.md IS identity.md — the
        # goal-lock must deny it. Inode identity catches the existing-file case;
        # the case-normalized fallback catches the to-be-created case.
        from arcagent.builtins.capabilities.write import write

        (tmp_path / "identity.md").write_text("operator goal\n")
        _runtime.configure(
            workspace=tmp_path,
            protected_paths=resolve_protected_paths(tmp_path, []),
        )
        with pytest.raises(ToolError) as exc:
            await write(file_path="IDENTITY.md", content="hijacked")
        assert exc.value.code == "TOOL_PROTECTED_PATH"
        assert (tmp_path / "identity.md").read_text() == "operator goal\n"

    async def test_write_through_traversal_to_identity_denied(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.write import write

        (tmp_path / "identity.md").write_text("operator goal\n")
        _runtime.configure(
            workspace=tmp_path,
            protected_paths=resolve_protected_paths(tmp_path, []),
        )
        with pytest.raises(ToolError) as exc:
            await write(file_path="sub/../identity.md", content="hijacked")
        assert exc.value.code == "TOOL_PROTECTED_PATH"
        assert (tmp_path / "identity.md").read_text() == "operator goal\n"

    async def test_write_through_symlink_to_identity_denied(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.write import write

        (tmp_path / "identity.md").write_text("operator goal\n")
        (tmp_path / "link.md").symlink_to(tmp_path / "identity.md")
        _runtime.configure(
            workspace=tmp_path,
            protected_paths=resolve_protected_paths(tmp_path, []),
        )
        # Denied either by the symlink guard or the protected-path guard; the
        # protected file must be untouched.
        with pytest.raises(ToolError):
            await write(file_path="link.md", content="hijacked")
        assert (tmp_path / "identity.md").read_text() == "operator goal\n"

    async def test_read_of_protected_file_still_works(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.read import read

        (tmp_path / "identity.md").write_text("goal text\n")
        _runtime.configure(
            workspace=tmp_path,
            protected_paths=resolve_protected_paths(tmp_path, []),
        )
        result = await read(file_path="identity.md")
        assert "goal text" in result


@pytest.mark.asyncio
class TestBashHostGuard:
    async def test_personal_bash_redirect_to_protected_denied(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.bash import bash

        (tmp_path / "identity.md").write_text("goal\n")
        sink = _RecordingSink()
        _runtime.configure(
            workspace=tmp_path,
            protected_paths=resolve_protected_paths(tmp_path, []),
            audit_sink=sink,
        )
        with pytest.raises(ToolError) as exc:
            await bash(command="echo x > identity.md")
        assert exc.value.code == "TOOL_PROTECTED_PATH"
        assert (tmp_path / "identity.md").read_text() == "goal\n"
        assert any(e == "tool.protected_path.denied" for e, _ in sink.events)

    async def test_personal_bash_normal_command_works(self, tmp_path: Path) -> None:
        from arcagent.builtins.capabilities.bash import bash

        _runtime.configure(
            workspace=tmp_path,
            protected_paths=resolve_protected_paths(tmp_path, []),
        )
        await bash(command="echo hello > note.txt")
        assert (tmp_path / "note.txt").read_text().strip() == "hello"
