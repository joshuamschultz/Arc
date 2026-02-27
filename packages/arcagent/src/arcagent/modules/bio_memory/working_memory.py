"""WorkingMemory — scratchpad lifecycle for bio-memory.

Manages ``memory/working.md``: overwritten every turn, cleared on session end.
Uses YAML frontmatter for structured metadata and atomic writes for crash safety.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

import yaml

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.utils.io import CHARS_PER_TOKEN, atomic_write_text
from arcagent.utils.sanitizer import sanitize_text


class WorkingMemory:
    """Working memory scratchpad — overwritten every turn, cleared on session end."""

    def __init__(self, memory_dir: Path, config: BioMemoryConfig) -> None:
        self._memory_dir = memory_dir
        self._config = config
        self._path = memory_dir / config.working_filename

    async def read(self) -> str:
        """Read current working.md content. Returns empty string if missing."""
        if not self._path.exists():
            return ""
        return await asyncio.to_thread(self._path.read_text, encoding="utf-8")

    async def write(
        self,
        content: str,
        frontmatter: dict[str, Any],
    ) -> None:
        """Overwrite working.md with frontmatter + body.

        Sanitizes content before writing (ASI-06 defense-in-depth).
        Enforces token budget: if body exceeds ``working_budget``,
        truncates to fit.
        """
        # Sanitize content before writing to disk (ASI-06)
        clean = sanitize_text(content, max_length=self._config.working_budget * CHARS_PER_TOKEN)
        max_chars = self._config.working_budget * CHARS_PER_TOKEN
        truncated = clean[:max_chars]

        fm_text = yaml.dump(
            frontmatter,
            default_flow_style=False,
            sort_keys=False,
        ).strip()
        parts = [f"---\n{fm_text}\n---", "", truncated, ""]
        await asyncio.to_thread(atomic_write_text, self._path, "\n".join(parts))

    async def clear(self) -> None:
        """Clear working.md (write empty frontmatter). Called on session end.

        Preserves the file for workspace detection — writes empty
        frontmatter with no body.
        """
        if not self._path.exists():
            return
        fm_text = yaml.dump({}, default_flow_style=False, sort_keys=False).strip()
        await asyncio.to_thread(
            atomic_write_text,
            self._path,
            f"---\n{fm_text}\n---\n",
        )

    def estimate_tokens(self, text: str) -> int:
        """Character-based token estimation using CHARS_PER_TOKEN."""
        if not text:
            return 0
        return math.ceil(len(text) / CHARS_PER_TOKEN)
