"""YAML frontmatter + markdown file I/O for entity files.

Atomic writes via tempfile + os.replace (matches FileBackend pattern).
File locks via fcntl.flock for concurrent access.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import frontmatter  # type: ignore[import-untyped]

from arcteam.memory.errors import EntityValidationError, LockTimeoutError
from arcteam.memory.types import EntityFile, EntityMetadata, IndexEntry

logger = logging.getLogger(__name__)

# Token estimation multiplier: words * 1.3 ≈ tokens
_TOKENS_PER_WORD = 1.3

# Max lock retries with backoff
_LOCK_MAX_RETRIES = 5

# Safe path component pattern: alphanumeric, hyphens, underscores, dots (no slashes)
_SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


class MemoryStorage:
    """YAML frontmatter + markdown file I/O for entity files.

    Atomic writes via tempfile + os.replace (matches FileBackend pattern).
    File locks via fcntl.flock for concurrent access.
    """

    def __init__(self, entities_dir: Path) -> None:
        self._entities_dir = entities_dir

    @property
    def entities_dir(self) -> Path:
        """Public accessor for entities directory path."""
        return self._entities_dir

    @staticmethod
    def validate_path_component(value: str, field_name: str) -> None:
        """Validate a string is safe for use as a path component.

        Prevents path traversal (e.g. '../../etc/passwd').
        Raises EntityValidationError on invalid input.
        """
        if not value:
            raise EntityValidationError(f"{field_name} must not be empty")
        if not _SAFE_PATH_RE.match(value):
            raise EntityValidationError(
                f"{field_name} contains invalid characters: {value!r} "
                f"(allowed: alphanumeric, hyphens, underscores, dots)"
            )

    def _entity_path(self, entity_type: str, entity_id: str) -> Path:
        """Compute path: entities/{entity_type}/{entity_id}.md

        Validates components to prevent path traversal.
        """
        self.validate_path_component(entity_type, "entity_type")
        self.validate_path_component(entity_id, "entity_id")
        path = self._entities_dir / entity_type / f"{entity_id}.md"
        # Defense-in-depth: verify resolved path is within entities_dir
        resolved = path.resolve()
        if not str(resolved).startswith(str(self._entities_dir.resolve())):
            raise EntityValidationError(f"Path traversal detected: {entity_type}/{entity_id}")
        return path

    def resolve_entity_path(self, relative_path: str) -> Path:
        """Resolve a relative entity path to absolute, with containment check."""
        path = self._entities_dir / relative_path
        resolved = path.resolve()
        if not str(resolved).startswith(str(self._entities_dir.resolve())):
            raise EntityValidationError(
                f"Path traversal detected in relative path: {relative_path!r}"
            )
        return path

    def estimate_tokens(self, text: str) -> int:
        """Character-based token estimation using word count heuristic."""
        return int(len(text.split()) * _TOKENS_PER_WORD)

    # --- Async interface (delegates to thread pool) ---

    async def read_entity(self, entity_id: str, index: dict[str, IndexEntry]) -> EntityFile | None:
        """Read entity file. Returns None if not found."""
        entry = index.get(entity_id)
        if entry is None:
            return None
        path = self.resolve_entity_path(entry.path)
        result = await asyncio.to_thread(self._sync_read, path)
        if result is None:
            return None
        meta_dict, content = result
        try:
            metadata = EntityMetadata.model_validate(meta_dict)
        except (ValueError, TypeError) as exc:
            logger.warning("Corrupted frontmatter in %s, skipping: %s", path, exc)
            return None
        return EntityFile(metadata=metadata, content=content)

    async def write_entity(self, entity_id: str, metadata: EntityMetadata, content: str) -> Path:
        """Atomic write: entity file with frontmatter + body. Returns written path."""
        path = self._entity_path(metadata.entity_type, entity_id)
        meta_dict = metadata.model_dump()
        await asyncio.to_thread(self._sync_write, path, meta_dict, content)
        return path

    async def delete_entity(self, entity_id: str, index: dict[str, IndexEntry]) -> bool:
        """Delete entity file. Returns True if existed."""
        entry = index.get(entity_id)
        if entry is None:
            return False
        path = self.resolve_entity_path(entry.path)
        return await asyncio.to_thread(self._sync_delete, path)

    async def read_frontmatter_only(self, path: Path) -> EntityMetadata | None:
        """Read YAML frontmatter without reading full body."""
        result = await asyncio.to_thread(self.read_frontmatter_sync, path)
        if result is None:
            return None
        try:
            return EntityMetadata.model_validate(result)
        except (ValueError, TypeError) as exc:
            logger.warning("Corrupted frontmatter in %s, skipping: %s", path, exc)
            return None

    async def list_entity_files(self) -> list[Path]:
        """Discover all .md files in entities directory tree."""
        return await asyncio.to_thread(self._sync_list_files)

    # --- Sync helpers (run via asyncio.to_thread) ---

    def _sync_read(self, path: Path) -> tuple[dict[str, Any], str] | None:
        """Read file, split frontmatter + body via python-frontmatter."""
        if not path.exists():
            return None
        try:
            post = frontmatter.load(str(path))
            return dict(post.metadata), post.content
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            logger.warning("Failed to read %s: %s", path, exc)
            return None

    def _sync_write(self, path: Path, metadata: dict[str, Any], content: str) -> None:
        """Atomic write via tempfile + os.replace. Uses fcntl.flock."""
        path.parent.mkdir(parents=True, exist_ok=True)
        post = frontmatter.Post(content, **metadata)
        serialized = frontmatter.dumps(post)

        # Acquire lock on target file (create if needed)
        with open(path, "a+b") as lock_fd:
            self._acquire_lock(lock_fd, path)
            try:
                fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(serialized)
                        f.write("\n")
                    os.replace(tmp, path)
                except BaseException:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                    raise
            finally:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)

    def _sync_delete(self, path: Path) -> bool:
        """Delete entity file."""
        if path.exists():
            path.unlink()
            return True
        return False

    def read_frontmatter_sync(self, path: Path) -> dict[str, Any] | None:
        """Read only YAML frontmatter block (between --- delimiters).

        Public sync method — used by IndexManager during rebuild.
        """
        if not path.exists():
            return None
        try:
            # Read first lines until we find closing ---
            lines: list[str] = []
            in_frontmatter = False
            with open(path, encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i == 0 and line.strip() == "---":
                        in_frontmatter = True
                        lines.append(line)
                        continue
                    if in_frontmatter:
                        lines.append(line)
                        if line.strip() == "---":
                            break
                    if i > 30:  # safety limit
                        break
            if not lines:
                return None
            text = "".join(lines)
            post = frontmatter.loads(text)
            return dict(post.metadata)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            logger.warning("Failed to read frontmatter from %s: %s", path, exc)
            return None

    def _sync_list_files(self) -> list[Path]:
        """List all .md files in entities directory tree."""
        if not self._entities_dir.exists():
            return []
        return sorted(self._entities_dir.rglob("*.md"))

    @staticmethod
    def _acquire_lock(fd: Any, path: Path) -> None:
        """Acquire exclusive file lock with retry and backoff."""
        import time

        for attempt in range(_LOCK_MAX_RETRIES):
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                time.sleep(0.1 * (attempt + 1))
        raise LockTimeoutError(str(path))
