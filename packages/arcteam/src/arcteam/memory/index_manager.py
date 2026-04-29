"""Manages _index.json — the entity lookup manifest.

Lazy rebuild via dirty flag. Writes touch .dirty marker.
Next read checks and rebuilds if dirty.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.errors import IndexCorruptionError
from arcteam.memory.storage import MemoryStorage
from arcteam.memory.types import IndexEntry

logger = logging.getLogger(__name__)


class IndexManager:
    """Manages _index.json — the entity lookup manifest."""

    def __init__(
        self,
        entities_dir: Path,
        memory_storage: MemoryStorage,
        config: TeamMemoryConfig,
    ) -> None:
        self._entities_dir = entities_dir
        self._storage = memory_storage
        self._config = config
        self._cache: dict[str, IndexEntry] | None = None
        self._dirty_path = entities_dir / ".dirty"
        self._index_path = entities_dir / "_index.json"
        self._checksum_path = entities_dir / "_index.sha256"

    def _is_dirty(self) -> bool:
        """Check if .dirty marker file exists."""
        return self._dirty_path.exists()

    async def touch_dirty(self) -> None:
        """Set dirty flag (called after writes)."""
        await asyncio.to_thread(self._sync_touch_dirty)

    async def get_index(self) -> dict[str, IndexEntry]:
        """Get current index. Rebuilds if dirty flag set.

        Raises IndexCorruptionError if the on-disk index fails integrity check.
        Silently rebuilding over a tampered index is not safe — callers must
        decide how to recover.
        """
        if self._cache is not None and not self._is_dirty():
            return self._cache
        # Try loading from disk first — IndexCorruptionError propagates
        if self._index_path.exists() and not self._is_dirty():
            index = await asyncio.to_thread(self._sync_load_index)
            if index is not None:
                self._cache = index
                return self._cache
        # No usable on-disk index — rebuild from entity files
        return await self.rebuild()

    async def lookup(self, entity_id: str) -> IndexEntry | None:
        """O(1) entity lookup by ID."""
        index = await self.get_index()
        return index.get(entity_id)

    async def entity_exists(self, entity_id: str) -> bool:
        """Check if entity exists in index."""
        index = await self.get_index()
        return entity_id in index

    async def get_backlinks(self, entity_id: str) -> list[str]:
        """Compute backlinks from index (entities that link TO this one)."""
        index = await self.get_index()
        entry = index.get(entity_id)
        if entry is None:
            return []
        return list(entry.linked_from)

    async def rebuild(self) -> dict[str, IndexEntry]:
        """Full index rebuild from entity frontmatter. Atomic write."""
        index = await asyncio.to_thread(self._sync_rebuild)
        self._cache = index
        return index

    # --- Sync helpers ---

    def _sync_touch_dirty(self) -> None:
        """Create .dirty marker file."""
        self._entities_dir.mkdir(parents=True, exist_ok=True)
        self._dirty_path.touch()

    def _sync_rebuild(self) -> dict[str, IndexEntry]:
        """Scan all entity files, read frontmatter, build index."""
        self._entities_dir.mkdir(parents=True, exist_ok=True)
        files = list(self._entities_dir.rglob("*.md"))
        index: dict[str, IndexEntry] = {}

        # Pass 1: read all entity frontmatter
        for path in files:
            meta = self._storage.read_frontmatter_sync(path)
            if meta is None:
                continue
            entity_id = meta.get("entity_id", "")
            if not entity_id:
                continue
            rel_path = str(path.relative_to(self._entities_dir))
            # Extract first line of body as summary snippet
            snippet = self._extract_snippet(path)
            index[entity_id] = IndexEntry(
                entity_id=entity_id,
                path=rel_path,
                entity_type=meta.get("entity_type", ""),
                tags=meta.get("tags", []),
                links_to=meta.get("links_to", []),
                summary_snippet=snippet,
                last_updated=meta.get("last_updated", ""),
                status=meta.get("status", "active"),
                classification=meta.get("classification", "unclassified"),
            )

        # Pass 2: compute linked_from (backlinks) from all links_to
        for entry in index.values():
            for target_id in entry.links_to:
                target = index.get(target_id)
                if target is not None:
                    target.linked_from.append(entry.entity_id)

        # Atomic write index
        self._sync_write_index(index)

        # Clear dirty flag
        if self._dirty_path.exists():
            self._dirty_path.unlink()

        return index

    def _sync_write_index(self, index: dict[str, IndexEntry]) -> None:
        """Atomic write _index.json via tempfile + os.replace. Writes SHA-256 checksum."""
        self._entities_dir.mkdir(parents=True, exist_ok=True)
        data = {eid: entry.model_dump() for eid, entry in index.items()}
        content = json.dumps(data, ensure_ascii=False, indent=2)

        # Write index file atomically
        fd, tmp = tempfile.mkstemp(dir=self._entities_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, self._index_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

        # Write SHA-256 checksum file
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._checksum_path.write_text(checksum, encoding="utf-8")

    def _sync_load_index(self) -> dict[str, IndexEntry] | None:
        """Load index from _index.json. Verifies SHA-256 checksum at all tiers.

        Tier-stringency on missing checksum file:
        - personal: missing = warn + continue (developer may have edited manually)
        - enterprise / federal: missing = hard error (IndexCorruptionError)

        When the checksum file IS present, it MUST validate at every tier.
        ADR-019 four-pillars-universal: tamper-evident integrity runs at all tiers.
        """
        if not self._index_path.exists():
            return None
        try:
            content = self._index_path.read_text(encoding="utf-8")
            self._apply_checksum_policy(content)
            data = json.loads(content)
            return {eid: IndexEntry.model_validate(entry) for eid, entry in data.items()}
        except IndexCorruptionError:
            raise
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("Failed to load _index.json, will rebuild: %s", exc)
            return None

    def _apply_checksum_policy(self, content: str) -> None:
        """Enforce SHA-256 checksum policy based on tier.

        Called every time the index is loaded from disk.
        """
        checksum_present = self._checksum_path.exists()

        if not checksum_present:
            if self._config.tier == "personal":
                # Personal tier: missing checksum is a warning, not a hard failure.
                # A solo developer may have edited the file directly.
                logger.warning(
                    "No _index.sha256 found at personal tier — skipping integrity check"
                )
                return
            # enterprise / federal: missing checksum = hard error
            raise IndexCorruptionError(f"checksum file missing for {self._config.tier} tier")

        # Checksum file is present — validate at every tier
        stored = self._checksum_path.read_text(encoding="utf-8").strip()
        computed = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if stored != computed:
            raise IndexCorruptionError(
                f"index checksum mismatch: stored={stored[:16]}... computed={computed[:16]}..."
            )

    @staticmethod
    def _extract_snippet(path: Path) -> str:
        """Extract first non-empty line after frontmatter as summary snippet."""
        try:
            in_frontmatter = False
            past_frontmatter = False
            with open(path, encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not in_frontmatter and stripped == "---":
                        in_frontmatter = True
                        continue
                    if in_frontmatter and stripped == "---":
                        past_frontmatter = True
                        continue
                    if past_frontmatter and stripped:
                        # Strip markdown heading markers
                        return stripped.lstrip("# ").strip()[:200]
            return ""
        except (OSError, UnicodeDecodeError):
            return ""
