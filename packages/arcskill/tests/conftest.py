"""Shared test doubles for arcskill.

``DirRevisionWriter`` stands in for the operator-anchored revision chain arcagent
injects (``arcagent``'s ``OperatorSkillRevisionWriter``). The improver never writes
a skill file itself; it hands every change to the writer. This double records each
commit and lays the files into the skill folder, so a test can assert both what was
committed and what the next read of the skill sees.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest


class DirRevisionWriter:
    """Record commits and materialize them under ``root_of(skill_name)``."""

    def __init__(self, root_of: Callable[[str], Path]) -> None:
        self._root_of = root_of
        self.commits: list[tuple[str, dict[str, bytes], str]] = []

    def commit(self, skill_name: str, files: Mapping[str, bytes], *, reason: str) -> str:
        changed = dict(files)
        self.commits.append((skill_name, changed, reason))
        root = self._root_of(skill_name)
        for relative, content in changed.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        joined = b"".join(path.encode() + data for path, data in sorted(changed.items()))
        return hashlib.sha256(joined).hexdigest()


@pytest.fixture
def dir_writer() -> type[DirRevisionWriter]:
    """The writer double's class, so a test can bind it to its own skill layout."""
    return DirRevisionWriter
