"""Tests for arcgateway.fs_reader — the single audited chokepoint for read-only fs access."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from arcgateway.fs_reader import (
    MAX_READ_BYTES,
    FileContent,
    FileEntry,
    FileTooLargeError,
    PathTraversalError,
    list_tree,
    read_file,
)


@pytest.fixture
def agent_root(tmp_path: Path) -> Path:
    """Build a synthetic agent directory layout."""
    root = tmp_path / "team" / "alice_agent"
    (root / "workspace" / "memory").mkdir(parents=True)
    (root / "workspace" / "policy.md").write_text(
        "- [P01] Test {score:5, uses:0, reviewed:2026-04-01, created:2026-04-01, source:s}\n",
        encoding="utf-8",
    )
    (root / "workspace" / "memory" / "note.md").write_text("# A note\n", encoding="utf-8")
    (root / "arcagent.toml").write_text("[agent]\nname = 'alice'\n", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    (root / "data.json").write_text('{"k": "v"}', encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Scope semantics
# ---------------------------------------------------------------------------


class TestScope:
    def test_team_scope_raises_not_implemented(self, agent_root: Path) -> None:
        with pytest.raises(NotImplementedError, match="team"):
            read_file(
                scope="team",
                agent_id="alice",
                agent_root=agent_root,
                rel_path="arcagent.toml",
                caller_did="did:test:user",
            )

    def test_shared_scope_raises_not_implemented(self, agent_root: Path) -> None:
        with pytest.raises(NotImplementedError, match="shared"):
            read_file(
                scope="shared",
                agent_id="alice",
                agent_root=agent_root,
                rel_path="arcagent.toml",
                caller_did="did:test:user",
            )

    def test_team_scope_raises_on_list_tree(self, agent_root: Path) -> None:
        with pytest.raises(NotImplementedError):
            list_tree(
                scope="team",
                agent_id="alice",
                agent_root=agent_root,
                caller_did="did:test:user",
            )

    def test_agent_scope_requires_root(self) -> None:
        with pytest.raises(ValueError, match="agent_root"):
            read_file(
                scope="agent",
                agent_id="alice",
                agent_root=None,
                rel_path="arcagent.toml",
                caller_did="did:test:user",
            )


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------


class TestPathTraversal:
    def test_relative_dotdot_blocked(self, agent_root: Path) -> None:
        with pytest.raises(PathTraversalError):
            read_file(
                scope="agent",
                agent_id="alice",
                agent_root=agent_root,
                rel_path="../../../etc/passwd",
                caller_did="did:test:user",
            )

    def test_absolute_path_blocked(self, agent_root: Path) -> None:
        with pytest.raises(PathTraversalError):
            read_file(
                scope="agent",
                agent_id="alice",
                agent_root=agent_root,
                rel_path="/etc/passwd",
                caller_did="did:test:user",
            )

    def test_symlink_escape_blocked(self, agent_root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = agent_root / "escape"
        link.symlink_to(outside)
        with pytest.raises(PathTraversalError):
            read_file(
                scope="agent",
                agent_id="alice",
                agent_root=agent_root,
                rel_path="escape",
                caller_did="did:test:user",
            )

    def test_traversal_via_subdir(self, agent_root: Path) -> None:
        with pytest.raises(PathTraversalError):
            read_file(
                scope="agent",
                agent_id="alice",
                agent_root=agent_root,
                rel_path="workspace/../../etc/passwd",
                caller_did="did:test:user",
            )


# ---------------------------------------------------------------------------
# read_file content handling
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_read_text_file(self, agent_root: Path) -> None:
        content = read_file(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            rel_path="arcagent.toml",
            caller_did="did:test:user",
        )
        assert isinstance(content, FileContent)
        assert content.content_type == "text"
        assert "alice" in content.content
        assert content.path == "arcagent.toml"

    def test_read_markdown(self, agent_root: Path) -> None:
        c = read_file(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            rel_path="workspace/policy.md",
            caller_did="did:test:user",
        )
        assert c.content_type == "text"
        assert "P01" in c.content

    def test_read_json_marked_as_json(self, agent_root: Path) -> None:
        c = read_file(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            rel_path="data.json",
            caller_did="did:test:user",
        )
        assert c.content_type == "json"

    def test_read_binary_returns_base64(self, agent_root: Path) -> None:
        c = read_file(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            rel_path="logo.png",
            caller_did="did:test:user",
        )
        assert c.content_type == "binary"
        # Round-trip the base64 to confirm.
        decoded = base64.b64decode(c.content)
        assert decoded.startswith(b"\x89PNG")

    def test_size_cap_enforced(self, agent_root: Path) -> None:
        large = agent_root / "big.md"
        large.write_bytes(b"x" * (MAX_READ_BYTES + 1))
        with pytest.raises(FileTooLargeError):
            read_file(
                scope="agent",
                agent_id="alice",
                agent_root=agent_root,
                rel_path="big.md",
                caller_did="did:test:user",
            )

    def test_missing_file_raises(self, agent_root: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_file(
                scope="agent",
                agent_id="alice",
                agent_root=agent_root,
                rel_path="does_not_exist.md",
                caller_did="did:test:user",
            )

    def test_directory_path_raises(self, agent_root: Path) -> None:
        # Pointing at a directory should fail (use list_tree instead).
        with pytest.raises(FileNotFoundError):
            read_file(
                scope="agent",
                agent_id="alice",
                agent_root=agent_root,
                rel_path="workspace",
                caller_did="did:test:user",
            )


# ---------------------------------------------------------------------------
# list_tree
# ---------------------------------------------------------------------------


class TestListTree:
    def test_returns_list_of_FileEntry(self, agent_root: Path) -> None:
        entries = list_tree(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            caller_did="did:test:user",
        )
        assert all(isinstance(e, FileEntry) for e in entries)
        assert len(entries) > 0

    def test_includes_files_and_dirs(self, agent_root: Path) -> None:
        entries = list_tree(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            caller_did="did:test:user",
        )
        types = {e.type for e in entries}
        assert "file" in types
        assert "dir" in types

    def test_paths_are_relative_to_root(self, agent_root: Path) -> None:
        entries = list_tree(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            caller_did="did:test:user",
        )
        for e in entries:
            assert not e.path.startswith("/")
            # No absolute paths leak through.
            assert ".." not in e.path

    def test_subdir_root(self, agent_root: Path) -> None:
        entries = list_tree(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            rel_path="workspace",
            caller_did="did:test:user",
        )
        names = [e.path for e in entries]
        # Paths are relative to agent_root, not the subdir.
        assert any(n.endswith("policy.md") for n in names)
        assert any(n.endswith("memory") for n in names)

    def test_max_depth_limits_recursion(self, agent_root: Path) -> None:
        # Build a deep nested tree.
        deep = agent_root / "deep"
        for i in range(15):
            deep = deep / f"level{i}"
        deep.mkdir(parents=True)
        (deep / "leaf.txt").write_text("leaf", encoding="utf-8")

        entries = list_tree(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            max_depth=3,
            caller_did="did:test:user",
        )
        # leaf.txt is way deeper than 3, must not appear.
        assert not any(e.path.endswith("leaf.txt") for e in entries)

    def test_skips_hidden_entries(self, agent_root: Path) -> None:
        (agent_root / ".secret").write_text("hidden", encoding="utf-8")
        entries = list_tree(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            caller_did="did:test:user",
        )
        assert not any(e.path == ".secret" for e in entries)

    def test_traversal_blocked_in_list_tree(self, agent_root: Path) -> None:
        with pytest.raises(PathTraversalError):
            list_tree(
                scope="agent",
                agent_id="alice",
                agent_root=agent_root,
                rel_path="../..",
                caller_did="did:test:user",
            )


# ---------------------------------------------------------------------------
# Read-only by structure
# ---------------------------------------------------------------------------


class TestReadOnlyByStructure:
    def test_no_write_methods_exposed(self) -> None:
        """The public surface MUST NOT include any write helpers."""
        from arcgateway import fs_reader

        public = [n for n in dir(fs_reader) if not n.startswith("_")]
        # Forbidden names — if any of these ever appear, the read-only invariant is broken.
        forbidden = {
            "write_file",
            "write_bytes",
            "write_text",
            "create_file",
            "mkdir",
            "make_dir",
            "rm",
            "remove",
            "delete",
            "delete_file",
            "rmtree",
            "rename",
            "move",
            "copy",
            "chmod",
            "touch",
            "append",
            "open_for_write",
        }
        leaks = forbidden & set(public)
        assert not leaks, f"fs_reader exposes write helpers: {leaks}"
