"""arcskill.lock -- HubLockFile schema and atomic I/O.

The lock file records every skill installed via the hub pipeline.  It is
written atomically (temp-file + os.replace) so a crash during write never
leaves a partial record.

Lock file location: ``~/.arc/skills/.hub/lock.json``

Schema
------
::

    {
        "version": 1,
        "skills": {
            "<name>": {
                "content_hash": "sha256...",
                "rekor_uuid": "...",
                "slsa_level": 3,
                "scan_verdict": "safe",
                "install_path": "/path/to/installed/skill",
                "files": ["skill.py", "MODULE.yaml"],
                "installed_at": "2026-04-18T12:00:00Z",
                "updated_at": "2026-04-18T12:00:00Z"
            }
        }
    }

Integrity
---------
The lock file is written atomically.  On load, if the JSON cannot be
parsed, ``HubLockFileCorrupted`` is raised.  Callers should not continue
with an unknown lock-file state at federal tier.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field


# HubLockFileCorrupted defined here to avoid circular import with hub/__init__.py
class HubLockFileCorrupted(Exception):  # noqa: N818
    """Raised when the lock file on disk cannot be parsed or fails integrity check."""


logger = logging.getLogger(__name__)

_LOCK_VERSION = 1


# ---------------------------------------------------------------------------
# SkillLockEntry
# ---------------------------------------------------------------------------


class SkillLockEntry(BaseModel):
    """One skill's record in the lock file.

    Attributes
    ----------
    content_hash:
        SHA-256 hex of the installed bundle at install time.
    rekor_uuid:
        Rekor transparency-log entry identifier (empty if verification
        was skipped at non-federal tiers).
    slsa_level:
        SLSA Build Level verified at install time.
    scan_verdict:
        Scanner verdict: ``"safe"`` | ``"caution"`` | ``"dangerous"``.
        Entries with ``"dangerous"`` should not exist -- install should
        have been blocked.
    install_path:
        Absolute path to the installed skill directory.
    files:
        List of files that were installed (relative to install_path).
    installed_at:
        ISO-8601 UTC timestamp of initial install.
    updated_at:
        ISO-8601 UTC timestamp of last update (equals installed_at
        on first install).
    quarantined:
        True if this skill has been quarantined due to a CRL hit.
    """

    content_hash: str
    rekor_uuid: str = ""
    slsa_level: int = 0
    scan_verdict: str = "safe"
    install_path: str = ""
    files: list[str] = Field(default_factory=list)
    installed_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    quarantined: bool = False


# ---------------------------------------------------------------------------
# HubLockFile
# ---------------------------------------------------------------------------


class HubLockFile(BaseModel):
    """Root lock file model.

    Attributes
    ----------
    version:
        Schema version for forward-compatibility.
    skills:
        Mapping of skill name → ``SkillLockEntry``.
    """

    version: int = _LOCK_VERSION
    skills: dict[str, SkillLockEntry] = Field(default_factory=dict)

    # -----------------------------------------------------------------------
    # I/O
    # -----------------------------------------------------------------------

    @classmethod
    def default_path(cls) -> Path:
        """Return the default lock file path: ``~/.arc/skills/.hub/lock.json``."""
        return Path.home() / ".arc" / "skills" / ".hub" / "lock.json"

    @classmethod
    def load(cls, path: Path | None = None) -> HubLockFile:
        """Load the lock file from *path*, or from the default path.

        Returns an empty ``HubLockFile`` if the file does not exist yet.

        Raises
        ------
        HubLockFileCorrupted
            If the file exists but cannot be parsed as valid JSON.
        """
        target = path or cls.default_path()
        if not target.exists():
            logger.debug("Lock file not found at %s; starting fresh", target)
            return cls()

        try:
            raw = target.read_text(encoding="utf-8")
            data = json.loads(raw)
            return cls.model_validate(data)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HubLockFileCorrupted(
                f"Lock file at {target} is corrupted and cannot be parsed: {exc}"
            ) from exc

    def save(self, path: Path | None = None) -> None:
        """Write the lock file atomically to *path* (or default path).

        Uses ``tempfile + os.replace`` so a crash during write never leaves
        a partial file.

        Parameters
        ----------
        path:
            Override path for tests.  Defaults to ``default_path()``.
        """
        target = path or self.default_path()
        target.parent.mkdir(parents=True, exist_ok=True)

        payload = self.model_dump_json(indent=2)

        # Atomic write: write to temp file in same directory, then replace.
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, prefix=".lock_", suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_path, target)
            # Restrict permissions: readable by owner only (no group/world).
            target.chmod(0o600)
        except Exception:
            # Cleanup the temp file on any error.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        logger.debug("Lock file written atomically to %s", target)

    # -----------------------------------------------------------------------
    # Mutation helpers
    # -----------------------------------------------------------------------

    def add_or_update(self, name: str, entry: SkillLockEntry) -> None:
        """Add or replace the entry for *name*.

        Sets ``updated_at`` to the current UTC time when updating an
        existing entry (preserves ``installed_at``).
        """
        existing = self.skills.get(name)
        if existing is not None:
            entry = entry.model_copy(update={"installed_at": existing.installed_at})
        self.skills[name] = entry

    def quarantine(self, name: str) -> bool:
        """Mark *name* as quarantined.

        Returns True if the entry existed, False if not found.
        """
        entry = self.skills.get(name)
        if entry is None:
            return False
        self.skills[name] = entry.model_copy(update={"quarantined": True})
        return True

    def remove(self, name: str) -> bool:
        """Remove *name* from the lock file.

        Returns True if the entry existed, False otherwise.
        """
        if name not in self.skills:
            return False
        del self.skills[name]
        return True

    def is_quarantined(self, name: str) -> bool:
        """Return True if *name* is in the lock file and quarantined."""
        entry = self.skills.get(name)
        return entry is not None and entry.quarantined

    def installed_names(self) -> list[str]:
        """Return names of all non-quarantined installed skills."""
        return [name for name, entry in self.skills.items() if not entry.quarantined]
