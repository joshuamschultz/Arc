"""Retention purge — whole rotated-file lifecycle management (SPEC-016 D-440).

Retention purges whole rotated ``traces-*.jsonl`` files, never live chain
lines. Two independent bounds apply in the same pass: files older than
``max_age_days`` are deleted outright, then the remaining rotated files
are deleted oldest-first while the directory exceeds ``max_bytes``. The
file matching today's date is never a purge candidate — it is always
excluded before either bound is applied, so purge can never race a
concurrent append landing on the live file.

Whole-file deletion means ``JSONLTraceStore.verify_chain()`` still
detects tampering *among the records present*, but — as documented in the
SDD Research Insights — it cannot prove that no earlier records were
removed; a hash chain alone cannot distinguish a policy purge from a
malicious truncation of the head (Crosby, tamper-evident logging). That
requires an external, comparable anchor: :func:`build_checkpoint` returns
a small manifest (head hash, record count, file inventory, timestamp)
that a higher layer signs and durably stores (arcllm owns capture, not
signed-chain anchoring — see CLAUDE.md "don't mix concerns"). A caller
wires ``JSONLTraceStore(checkpoint_sink=...)`` to an external signer, e.g.
arctrust's ``WormSink`` via ``emit(AuditEvent(action="trace.checkpoint",
extra=checkpoint), sink)``. Later, :func:`verify_against_anchor` checks
whether a live store still contains an anchored ``head_hash`` — proving
it was not rolled back to before that anchor. Legitimate retention purge
only ever deletes the OLDEST files, so the most recently anchored head
survives it; a malicious rollback past the last anchor removes it.

Residual, documented honestly: activity between two anchors — down to
the last anchored head — can still be deleted undetected. That window is
bounded by anchor frequency (this module anchors at every rotation, i.e.
daily, when a ``checkpoint_sink`` is wired).
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Bounds a single purge pass so a directory with thousands of rotated
# files cannot monopolize the event loop in one call (scheduler hardening).
_DEFAULT_MAX_FILES_PER_RUN = 1000


def _file_date(path: Path) -> str:
    """Return the YYYY-MM-DD date encoded in a ``traces-*.jsonl`` filename."""
    return path.stem.replace("traces-", "")


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _rotated_candidates(traces_dir: Path) -> list[Path]:
    """List rotated files, oldest-first, excluding today's live file.

    Re-globbed fresh on every call (never cached) and re-anchored to a
    freshly computed "today" so a purge can never delete the file an
    append is concurrently landing on.
    """
    today = _today()
    return sorted(
        (p for p in traces_dir.glob("traces-*.jsonl") if _file_date(p) < today),
        key=_file_date,
    )


def _dir_size_bytes(traces_dir: Path) -> int:
    """Total size of all trace files (rotated + live) currently on disk."""
    return sum(p.stat().st_size for p in traces_dir.glob("traces-*.jsonl") if p.exists())


def _safe_delete(path: Path) -> bool:
    """Delete one file, logging (not raising) on failure.

    ``except Exception`` — never ``BaseException`` — so a purge failure
    is observable but can never suppress cancellation/shutdown signals.
    """
    try:
        path.unlink()
    except Exception:
        logger.warning("Failed to purge trace file %s", path, exc_info=True)
        return False
    return True


async def purge(
    traces_dir: Path,
    *,
    max_age_days: int | None,
    max_bytes: int | None,
    max_files_per_run: int = _DEFAULT_MAX_FILES_PER_RUN,
) -> list[Path]:
    """Delete whole rotated trace files past the age/size bounds.

    Args:
        traces_dir: Directory containing ``traces-*.jsonl`` files.
        max_age_days: Delete rotated files strictly older than this many
            days. ``None`` disables the age bound.
        max_bytes: While the directory exceeds this size, delete the
            oldest remaining rotated file. ``None`` disables the size
            bound.
        max_files_per_run: Upper bound on deletions in a single call, so
            a very large backlog cannot block the event loop.

    Returns:
        Paths actually deleted, oldest-first.
    """
    if max_age_days is None and max_bytes is None:
        return []

    deleted: list[Path] = []
    candidates = _rotated_candidates(traces_dir)

    if max_age_days is not None:
        cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
        survivors: list[Path] = []
        for path in candidates:
            if len(deleted) >= max_files_per_run:
                survivors.append(path)
                continue
            if _file_date(path) < cutoff and _safe_delete(path):
                deleted.append(path)
            else:
                survivors.append(path)
        candidates = survivors

    if max_bytes is not None:
        remaining = list(candidates)
        while (
            remaining
            and len(deleted) < max_files_per_run
            and _dir_size_bytes(traces_dir) > max_bytes
        ):
            oldest = remaining.pop(0)
            if _safe_delete(oldest):
                deleted.append(oldest)

    return deleted


def build_checkpoint(traces_dir: Path) -> dict[str, Any]:
    """Build an observability manifest: head hash, record count, file list.

    Not itself cryptographically signed — exporting this manifest to an
    external signed anchor, e.g. arctrust's ``WormSink``, is a downstream
    concern (arctrust owns signing/anchoring; arcllm owns capture — see
    CLAUDE.md "don't mix concerns"). Comparing two checkpoints taken over
    time reveals a prefix purge or truncation that ``verify_chain()`` alone
    cannot see, because a shrinking ``files`` list or a discontinuous
    ``head_hash`` is directly observable even though the chain still
    self-verifies. See :func:`verify_against_anchor` for the read-side
    check against a single anchored checkpoint.
    """
    files = sorted(traces_dir.glob("traces-*.jsonl"))
    record_count = 0
    head_hash = "0" * 64
    for file_path in files:
        for line in file_path.read_text().strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            record_count += 1
            head_hash = data.get("record_hash", head_hash)
    return {
        "head_hash": head_hash,
        "record_count": record_count,
        "files": [f.name for f in files],
        "timestamp": datetime.now(UTC).isoformat(),
    }


def verify_against_anchor(traces_dir: Path, anchor: dict[str, Any]) -> bool:
    """Check whether a live store still contains a previously anchored head.

    THE GUARANTEE: at anchor time, a signed WORM record immutably captured
    the store's ``head_hash`` (see :func:`build_checkpoint`). This function
    proves the live store was NOT rolled back to before that anchor by
    confirming ``anchor["head_hash"]`` still appears as some live record's
    ``record_hash``. Legitimate retention purge only deletes the OLDEST
    rotated files, so a recently anchored head survives it (tolerated,
    returns True). A malicious rollback/truncation past the last anchor
    removes that record entirely (detected, returns False) — even though
    the store's own ``verify_chain()`` still passes over what remains,
    because that only proves internal consistency of records present.

    Deliberately NOT checked: ``record_count`` non-decreasing or ``files``
    superset. Both shrink on every legitimate purge, so either check would
    false-positive as tampering on routine retention. Head-hash presence
    is the only invariant that distinguishes "purged" from "rolled back."

    Residual (documented honestly): activity between anchors, down to the
    last anchored head, can still be deleted undetected — this function
    cannot see it. That window is bounded only by anchor frequency (this
    module anchors at every rotation when a ``checkpoint_sink`` is wired).

    Args:
        traces_dir: Directory containing ``traces-*.jsonl`` files.
        anchor: A checkpoint dict as returned by :func:`build_checkpoint`
            (typically read back via arctrust's
            ``read_verified_anchor(...)``).

    Returns:
        True if ``anchor["head_hash"]`` is the genesis all-zero hash
        (nothing was anchored yet — vacuously nothing to attest) or is
        still present among live records. False if it has been rolled
        back past.
    """
    anchor_head = anchor.get("head_hash")
    if anchor_head == "0" * 64:
        return True
    for file_path in sorted(traces_dir.glob("traces-*.jsonl")):
        for line in file_path.read_text().strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("record_hash") == anchor_head:
                return True
    return False


__all__ = [
    "build_checkpoint",
    "purge",
    "verify_against_anchor",
]
