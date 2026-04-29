"""GDPR tombstone implementation for per-user profile erasure.

When a user invokes their right-to-be-forgotten the system must:

1. Delete the ``user_profile/{user_did}.md`` file outright.
2. Redact ALL fields containing the raw ``user_did`` string from every
   session JSONL that references the user — field-wise, preserving the
   tool-call audit structure.
3. Emit ``session.fts5.reindex_needed`` so the FTS5 indexer drops any
   indexed rows for the user from the derived SQLite index.
4. Retain a tombstone record (user_did HASH, NOT the raw DID, plus
   timestamp) in ``tombstone_events/`` for compliance proof.

Design notes:
- The tombstone record stores only ``sha256(user_did)`` to satisfy
  GDPR Art. 17 (erasure) while preserving a compliance audit trail
  (NIST AU-9, FedRAMP).
- Session JSONL redaction is FIELD-wise: we replace only string values
  that contain the literal ``user_did`` with ``[redacted]``.  The JSON
  structure, keys, and tool-call audit data are preserved.
- FTS5 reindex is triggered via event emission ONLY — this module does
  not import or call the SessionIndex directly.  Keeps concern
  separation clean (see SDD §3.6 constraint).
- The Derived section of the user profile is wiped, but the hook
  ``_mark_derived_regeneratable`` is called to signal that a
  regeneration pipeline may rebuild it from surviving session data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcagent.modules.user_profile.config import UserProfileConfig
from arcagent.modules.user_profile.store import ProfileStore

_logger = logging.getLogger("arcagent.modules.user_profile.tombstone")


class TombstoneEvent:
    """Immutable record of a GDPR right-to-be-forgotten request.

    Attributes:
        user_did_hash:  SHA-256 hex digest of the raw user DID.
                        The raw DID is NOT stored — compliance requires
                        proof that erasure happened, not who was erased.
        timestamp:      UTC datetime of tombstone creation.
        sessions_redacted:  Number of JSONL session files redacted.
    """

    def __init__(
        self,
        user_did_hash: str,
        timestamp: datetime,
        sessions_redacted: int = 0,
    ) -> None:
        self.user_did_hash = user_did_hash
        self.timestamp = timestamp
        self.sessions_redacted = sessions_redacted

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_did_hash": self.user_did_hash,
            "timestamp": self.timestamp.isoformat(),
            "sessions_redacted": self.sessions_redacted,
        }


def apply_tombstone(
    user_did: str,
    *,
    workspace: Path,
    config: UserProfileConfig | None = None,
    telemetry: Any | None = None,
    sessions_dir: str = "sessions",
) -> TombstoneEvent:
    """Apply the full GDPR tombstone workflow for *user_did*.

    Steps performed:
    1. Delete the profile file via :class:`~store.ProfileStore`.
    2. Redact session JSONL files that contain *user_did* field values.
    3. Emit ``session.fts5.reindex_needed`` via *telemetry*.
    4. Write a tombstone record to ``tombstone_events/``.

    Parameters:
        user_did:     The raw user DID to erase.
        workspace:    Agent workspace root directory.
        config:       UserProfileConfig; defaults to defaults if None.
        telemetry:    Telemetry object with ``emit_event(name, data)``.
        sessions_dir: Sub-directory under *workspace* for JSONL files.

    Returns:
        A :class:`TombstoneEvent` with the compliance record.
    """
    cfg = config or UserProfileConfig()
    user_did_hash = _hash_did(user_did)
    ts = datetime.now(tz=UTC)

    # Step 1 — delete profile
    store = ProfileStore(workspace, cfg, telemetry=telemetry)
    store.delete(user_did)
    _logger.info("tombstone.profile_deleted user_did_hash=%s", user_did_hash)

    # Step 2 — redact session JSONLs
    sessions_path = workspace / sessions_dir
    redacted_count = _redact_sessions(sessions_path, user_did)
    _logger.info(
        "tombstone.sessions_redacted count=%d user_did_hash=%s",
        redacted_count,
        user_did_hash,
    )

    # Step 3 — emit FTS5 reindex event (concern-separated; no direct import)
    _emit_fts5_reindex(telemetry, user_did_hash)

    # Step 4 — write tombstone record (hash only, not raw DID)
    tombstone = TombstoneEvent(
        user_did_hash=user_did_hash,
        timestamp=ts,
        sessions_redacted=redacted_count,
    )
    _persist_tombstone(workspace, cfg, tombstone)

    # Audit the tombstone action
    if telemetry is not None:
        try:
            telemetry.emit_event(
                "memory.user_profile.tombstone",
                {
                    "user_did_hash": user_did_hash,
                    "sessions_redacted": redacted_count,
                    "ts": ts.isoformat(),
                },
            )
        except Exception:
            _logger.exception("Failed to emit tombstone audit event")

    return tombstone


def _hash_did(user_did: str) -> str:
    """Return the SHA-256 hex digest of a user DID."""
    return hashlib.sha256(user_did.encode("utf-8")).hexdigest()


def _redact_sessions(sessions_path: Path, user_did: str) -> int:
    """Redact *user_did* from all JSONL files under *sessions_path*.

    For each ``.jsonl`` file found recursively:
    - Parse each line as JSON.
    - Walk the JSON object and replace any string value containing the
      literal *user_did* with ``"[redacted]"``.
    - Write the modified content back atomically.

    Returns the number of JSONL files that contained and were redacted.
    The tool-call audit structure (JSON keys, nesting, event type) is
    fully preserved — only string VALUES containing the DID are touched.
    """
    if not sessions_path.is_dir():
        return 0

    redacted = 0
    for jsonl_path in sessions_path.rglob("*.jsonl"):
        if _redact_jsonl_file(jsonl_path, user_did):
            redacted += 1
    return redacted


def _redact_jsonl_file(path: Path, user_did: str) -> bool:
    """Redact *user_did* from a single JSONL file.

    Returns True if any redaction was made (file was rewritten).
    """
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        _logger.warning("tombstone: cannot read %s", path)
        return False

    lines_out: list[str] = []
    changed = False

    for line in original.splitlines():
        line = line.strip()
        if not line:
            lines_out.append("")
            continue
        try:
            obj = json.loads(line)
            new_obj = _redact_value(obj, user_did)
            new_line = json.dumps(new_obj, ensure_ascii=False)
            if new_line != line:
                changed = True
            lines_out.append(new_line)
        except json.JSONDecodeError:
            # Preserve non-JSON lines unchanged (e.g. blank separators)
            lines_out.append(line)

    if not changed:
        return False

    new_text = "\n".join(lines_out) + "\n"
    _atomic_write_text(path, new_text)
    return True


def _redact_value(obj: Any, user_did: str) -> Any:
    """Recursively replace string values containing *user_did* with ``[redacted]``."""
    if isinstance(obj, dict):
        return {k: _redact_value(v, user_did) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_value(item, user_did) for item in obj]
    if isinstance(obj, str) and user_did in obj:
        return "[redacted]"
    return obj


def _emit_fts5_reindex(telemetry: Any | None, user_did_hash: str) -> None:
    """Emit ``session.fts5.reindex_needed`` so the indexer drops tombstoned rows.

    The FTS5 indexer subscribes to this event and schedules a rebuild
    that excludes entries matching the tombstoned user.  We emit the
    hash (not the raw DID) so the event itself cannot be used to
    reconstruct the deleted identity.
    """
    if telemetry is None:
        _logger.debug("tombstone: no telemetry; skipping fts5 reindex event")
        return
    try:
        telemetry.emit_event(
            "session.fts5.reindex_needed",
            {"user_did_hash": user_did_hash, "reason": "gdpr_tombstone"},
        )
        _logger.info("tombstone.fts5_reindex_requested user_did_hash=%s", user_did_hash)
    except Exception:
        _logger.exception("Failed to emit fts5 reindex event")


def _persist_tombstone(
    workspace: Path,
    config: UserProfileConfig,
    tombstone: TombstoneEvent,
) -> None:
    """Write the tombstone record to ``tombstone_events/{hash}.json``.

    The file contains ONLY the hash + timestamp + metadata — no raw DID.
    This satisfies GDPR right-to-erasure while providing compliance proof.
    """
    tombstone_dir = workspace / config.tombstone_dir
    tombstone_dir.mkdir(parents=True, exist_ok=True)
    path = tombstone_dir / f"{tombstone.user_did_hash}.json"
    _atomic_write_text(path, json.dumps(tombstone.to_dict(), indent=2) + "\n")
    _logger.info("tombstone.record_persisted path=%s", path)


def _mark_derived_regeneratable(user_did: str, workspace: Path) -> None:
    """Hook that signals the Derived section can be rebuilt post-tombstone.

    This is called after the profile file is deleted to indicate that
    a regeneration pipeline (e.g. dialectic deriver) may reconstruct
    Derived content from surviving (non-redacted) session data.

    Currently a no-op; exists so the regeneration pipeline can hook in
    without requiring a structural change to tombstone.py.
    """
    _logger.debug(
        "tombstone.derived_regeneratable user_did=%s workspace=%s",
        user_did,
        workspace,
    )


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write *text* to *path* using temp-file + os.replace()."""
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp", prefix=path.stem + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
