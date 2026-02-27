"""DailyNotes — append-only daily journal for bio-memory.

One file per day at ``memory/daily-notes/YYYY-MM-DD.md``.
Entries are appended with timestamps, creating a queryable history
that agents can reference indefinitely.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import yaml

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.utils.io import atomic_write_text
from arcagent.utils.sanitizer import sanitize_text


class DailyNotes:
    """Append-only daily journal. One file per day, timestamped entries."""

    def __init__(self, memory_dir: Path, config: BioMemoryConfig) -> None:
        self._dir = memory_dir / config.daily_notes_dirname
        self._config = config
        self._lock = asyncio.Lock()

    @property
    def directory(self) -> Path:
        """Return the daily-notes directory path."""
        return self._dir

    async def append(self, entries: list[str], agent_id: str = "") -> Path:
        """Append timestamped entries to today's note. Creates file if needed.

        Uses asyncio.Lock to prevent lost updates from concurrent appends.
        Returns the path to the daily note file.
        """
        async with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            path = self._dir / f"{today}.md"

            timestamp = datetime.now(UTC).strftime("%H:%M UTC")

            if not path.exists():
                frontmatter = {"date": today, "agent": agent_id}
                fm_text = yaml.dump(
                    frontmatter,
                    default_flow_style=False,
                    sort_keys=False,
                ).strip()
                content = f"---\n{fm_text}\n---\n\n# {today}\n\n"
            else:
                content = await asyncio.to_thread(
                    path.read_text,
                    encoding="utf-8",
                )

            new_lines = [f"\n## {timestamp}\n"]
            for entry in entries:
                clean = sanitize_text(entry, max_length=1000)
                new_lines.append(f"- {clean}\n")

            content += "".join(new_lines)
            await asyncio.to_thread(atomic_write_text, path, content)
            return path

    async def read_today(self) -> str:
        """Read today's note. Returns empty string if none exists."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        return await self.read_date(today)

    async def read_date(self, date_str: str) -> str:
        """Read a specific date's note. Returns empty string if not found."""
        path = self._dir / f"{date_str}.md"
        if not path.exists():
            return ""
        return await asyncio.to_thread(path.read_text, encoding="utf-8")
