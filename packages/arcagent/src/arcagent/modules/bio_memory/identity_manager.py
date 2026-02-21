"""IdentityManager — how-i-work.md lifecycle for bio-memory.

Manages the agent's learned behavioral patterns file. Read at session start,
injected into prompts, updated during consolidation. Emits audit events
for identity changes (NIST 800-53 AU-2).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.utils.io import CHARS_PER_TOKEN, atomic_write_text
from arcagent.utils.sanitizer import sanitize_text


class IdentityManager:
    """Identity file (how-i-work.md) — read at start, updated during consolidation."""

    def __init__(
        self,
        memory_dir: Path,
        config: BioMemoryConfig,
        telemetry: Any,
    ) -> None:
        self._memory_dir = memory_dir
        self._config = config
        self._telemetry = telemetry
        self._path = memory_dir / config.identity_filename

    async def read(self) -> str:
        """Read how-i-work.md. Returns empty string for new agents."""
        if not self._path.exists():
            return ""
        return self._path.read_text(encoding="utf-8")

    async def inject_context(self) -> str:
        """Read and format for prompt injection. Enforces identity_budget."""
        content = await self.read()
        if not content:
            return ""
        max_chars = self._config.identity_budget * CHARS_PER_TOKEN
        return content[:max_chars]

    async def update(self, new_content: str) -> None:
        """Write updated identity. Sanitizes content, emits audit event. Uses atomic write."""
        before = await self.read()
        # Defense-in-depth: sanitize even though consolidator also sanitizes (ASI-06)
        clean = sanitize_text(new_content, max_length=10000)
        atomic_write_text(self._path, clean)
        self._telemetry.audit_event(
            "identity.modified",
            details={
                "before_length": len(before),
                "after_length": len(new_content),
            },
        )

    def is_over_budget(self, content: str) -> bool:
        """Check if content exceeds identity_budget tokens."""
        if not content:
            return False
        tokens = math.ceil(len(content) / CHARS_PER_TOKEN)
        return tokens > self._config.identity_budget
