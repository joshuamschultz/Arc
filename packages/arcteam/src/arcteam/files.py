"""Team file storage — shared file exchange between agents.

Provides secure, audited file storage in the team's shared directory.
Each agent gets an isolated subdirectory under ``shared/files/{agent_name}/``.

Self-contained in arcteam — arcagent registers this as an LLM-callable tool.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

_logger = logging.getLogger("arcteam.files")

# Reject anything that could escape the team root
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_\-./]+$")


def _validate_path_within(path: Path, root: Path) -> None:
    """Ensure resolved path is inside root. Prevents path traversal."""
    resolved = path.resolve()
    root_resolved = root.resolve()
    if not str(resolved).startswith(str(root_resolved)):
        raise ValueError(f"Path escapes team root: {path}")


class TeamFileStore:
    """Store and retrieve files in the team's shared directory.

    Directory layout::

        {team_root}/
            shared/
                files/
                    {agent_name}/
                        2025-02-27_report.pdf
                        2025-02-27_data.csv

    Args:
        team_root: Root directory for the team (contains shared/).
    """

    def __init__(self, team_root: Path) -> None:
        self._team_root = team_root
        self._shared_dir = team_root / "shared" / "files"

    @property
    def shared_dir(self) -> Path:
        return self._shared_dir

    async def store(
        self,
        source_path: Path,
        agent_name: str,
    ) -> dict[str, Any]:
        """Copy a file into the team's shared directory for an agent.

        Args:
            source_path: Path to the source file (must exist).
            agent_name: Name of the agent storing the file.

        Returns:
            Dict with destination path, filename, and size.

        Raises:
            FileNotFoundError: If source_path doesn't exist.
            ValueError: If agent_name contains unsafe characters or
                source_path would escape boundaries.
        """
        if not _SAFE_NAME.match(agent_name):
            raise ValueError(f"Unsafe agent name: {agent_name!r}")

        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Source file not found: {source}")

        dest_dir = self._shared_dir / agent_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        _validate_path_within(dest_dir, self._team_root)

        dest = dest_dir / source.name

        # Avoid overwriting — append suffix if needed
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{source.stem}_{counter}{source.suffix}"
            counter += 1

        shutil.copy2(source, dest)
        size = dest.stat().st_size

        _logger.info(
            "Stored team file: %s → %s (%d bytes)",
            source.name,
            dest,
            size,
        )

        return {
            "path": str(dest),
            "filename": dest.name,
            "size": size,
            "agent": agent_name,
        }

    async def list_files(
        self,
        agent_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List files in the shared directory.

        Args:
            agent_name: Filter to a specific agent's files.
                If None, lists all shared files.

        Returns:
            List of dicts with path, filename, size, and agent.
        """
        results: list[dict[str, Any]] = []

        if agent_name:
            if not _SAFE_NAME.match(agent_name):
                raise ValueError(f"Unsafe agent name: {agent_name!r}")
            search_dir = self._shared_dir / agent_name
            if not search_dir.exists():
                return []
            for f in sorted(search_dir.iterdir()):
                if f.is_file():
                    results.append({
                        "path": str(f),
                        "filename": f.name,
                        "size": f.stat().st_size,
                        "agent": agent_name,
                    })
        else:
            if not self._shared_dir.exists():
                return []
            for agent_dir in sorted(self._shared_dir.iterdir()):
                if agent_dir.is_dir():
                    for f in sorted(agent_dir.iterdir()):
                        if f.is_file():
                            results.append({
                                "path": str(f),
                                "filename": f.name,
                                "size": f.stat().st_size,
                                "agent": agent_dir.name,
                            })

        return results
