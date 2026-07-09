"""Structured audit event schema, sinks, and emit function.

Every security-relevant action in Arc emits an AuditEvent. This module
provides the Pydantic schema, pluggable sinks, and a safe emit() function
that swallows sink failures so auditing never breaks the calling path.

NIST 800-53 AU-2 / AU-9 / AU-10 / AU-11 compliance:
- AuditEvent schema captures actor, action, target, outcome, timestamp.
- WormSink is the single durable, append-only, Ed25519-signed hash-chained
  audit log: one durable, tamper-evident write-once record (replaces the old
  unchained JsonlSink and the in-memory-only SignedChainSink). It survives
  process restarts, signs every record (AU-10 non-repudiation), and detects
  any byte mutation, forged signature, or sequence gap on verify.
- NullSink is used in tests and air-gapped evaluation where no sink is needed.
- emit() swallows sink exceptions — AU-5 (audit failure response): log locally
  but never propagate to caller.
- read_verified_anchor() closes a gap verify_chain() cannot: verify_chain()
  only proves internal consistency of the records *present* on a chain, not
  that none were removed from the head. read_verified_anchor() independently
  verifies the chain, then returns the newest anchored checkpoint payload
  (an ordinary AuditEvent with action="trace.checkpoint") for a caller to
  compare against a live store's current head (see
  arcllm.trace_retention.verify_against_anchor).

Extension pattern:
  Implement a class with ``write(event: AuditEvent) -> None`` and pass it
  to emit(). The AuditSink Protocol ensures type checking without coupling
  to a concrete class.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# fcntl is POSIX-only. The WormSink single-writer lock and 0o600 hardening are
# Unix deployment guarantees (federal/enterprise run on Linux); Windows imports
# arctrust only so arcllm stays cross-platform, so the lock is skipped there.
if sys.platform != "win32":
    import fcntl

from pydantic import BaseModel, ConfigDict, model_validator

from arctrust.signer import ED25519, Signer, verify_signature

_logger = logging.getLogger("arctrust.audit")

GENESIS_PREV_HASH = "0" * 64
"""prev_hash of the first record in a chain — the genesis anchor."""


# ---------------------------------------------------------------------------
# AuditEvent schema
# ---------------------------------------------------------------------------


class AuditEvent(BaseModel):
    """Immutable structured audit event.

    Carries all fields required to answer: who did what to what, when,
    and with what outcome. Optional fields support classification-aware
    and request-correlated deployments.

    Pydantic model — frozen so events cannot be mutated after creation.
    """

    model_config = ConfigDict(frozen=True)

    actor_did: str
    """DID of the entity performing the action."""

    action: str
    """Dotted event name (e.g. ``tool.call``, ``policy.evaluate``)."""

    target: str
    """The resource or tool being acted upon."""

    outcome: str
    """Result: ``allow``, ``deny``, ``error``, or domain-specific value."""

    classification: str | None = None
    """Data classification level (e.g. ``UNCLASSIFIED``, ``SECRET``)."""

    tier: str | None = None
    """Deployment tier (``personal``, ``enterprise``, ``federal``)."""

    request_id: str | None = None
    """Correlation ID for distributed tracing."""

    payload_hash: str | None = None
    """SHA-256 hex of the request payload (tamper evidence, no raw data)."""

    ts: str | None = None
    """ISO 8601 UTC timestamp. Auto-populated if omitted."""

    extra: dict[str, Any] = {}

    @model_validator(mode="after")
    def _set_ts(self) -> AuditEvent:
        # Populate timestamp at creation if not provided.
        # Use object.__setattr__ because the model is frozen.
        if self.ts is None:
            ts = datetime.now(UTC).isoformat()
            object.__setattr__(self, "ts", ts)
        return self


# ---------------------------------------------------------------------------
# AuditSink Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AuditSink(Protocol):
    """Protocol for audit event sinks.

    Any object with a ``write(event: AuditEvent) -> None`` method satisfies
    this protocol. Type checkers will verify conformance without inheritance.
    """

    def write(self, event: AuditEvent) -> None: ...


# ---------------------------------------------------------------------------
# NullSink — for tests and air-gapped evaluation
# ---------------------------------------------------------------------------


class NullSink:
    """No-op audit sink. Events are discarded immediately.

    Use in tests or evaluation contexts where no audit trail is needed.
    ``records`` is always empty — callers should not depend on it.
    """

    @property
    def records(self) -> list[dict[str, Any]]:
        """Always empty — NullSink discards all events."""
        return []

    def write(self, event: AuditEvent) -> None:
        """Discard the event silently."""


# ---------------------------------------------------------------------------
# WormSink — durable, append-only, Ed25519-signed hash chain (FR-1)
# ---------------------------------------------------------------------------


def _canonical_event_hash(*, seq: int, prev_hash: str, event: dict[str, Any]) -> str:
    """Deterministic SHA-256 over (seq, prev_hash, event).

    Uses ``sort_keys=True, ensure_ascii=True`` on a JSON-serialisable event dump
    (``model_dump(mode="json")``) — RFC-8785-equivalent for the ASCII-only
    AuditEvent schema, with no extra dependency. The hash commits the link
    (prev_hash), the position (seq), and the content (event), so any of the
    three changing is detectable.
    """
    payload = json.dumps(
        {"seq": seq, "prev_hash": prev_hash, "event": event},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class WormSink:
    """Durable, append-only, Ed25519-signed hash-chained audit log.

    The single compliance system of record. Each ``write()`` appends one JSON
    line ``{seq, event, prev_hash, event_hash, signature}`` to an append-only
    ``0600`` file. The hash chain links each entry to the previous and commits
    its monotonic ``seq``; the entry hash is Ed25519-signed (AU-10). The chain
    is restored from the file on construction, so it survives process restarts
    (unlike the old in-memory-only chain).

    Guarantees enforced:
    - **Restart-safe** — ``chain_tip``/``seq`` are recovered from the file tail.
    - **Tamper-evident** — ``verify_chain()`` checks hash links, the Ed25519
      signature of every record, sequence contiguity, and the genesis anchor.
    - **Single-writer** — an exclusive ``flock`` is held for the sink's lifetime;
      a second writer on the same active file raises (forked chains are an
      integrity hazard because the tip is held in memory).
    - **Crash-recoverable** — a torn final line from a mid-append crash is
      truncated and an explicit signed ``audit.worm.recovery`` record appended
      (silent truncation is indistinguishable from adversarial truncation).
    - **Fail-open** — ``write()`` swallows and logs IO errors (AU-5).
    - **Bounded** — the active file rotates to ``<stem>.<NNN><suffix>`` segments
      at ``max_records``/``max_bytes`` so verification streams rather than
      holding the whole chain in RAM.

    Args:
        path: Active chain file. Rotated segments live beside it.
        signer: The operator :class:`~arctrust.signer.Signer` (in-process or
            vault-transit) that signs each record's ``event_hash``. Under
            vault-transit custody the operator seed never enters this process
            (SPEC-037 REQ-006).
        genesis_tip: Expected ``prev_hash`` of the very first record. Defaults
            to the all-zero genesis anchor; supply an out-of-band value to
            detect head replacement (anti-genesis-substitution).
        max_records: Records per segment before rotation.
        max_bytes: Active-file size before rotation.
    """

    _FILE_MODE = 0o600
    _RECOVERY_ACTION = "audit.worm.recovery"

    def __init__(
        self,
        path: Path,
        signer: Signer,
        *,
        genesis_tip: str = GENESIS_PREV_HASH,
        max_records: int = 100_000,
        max_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._path = Path(path)
        self._signer = signer
        self._public_key = signer.public_key
        self._algorithm = signer.algorithm
        self._genesis_tip = genesis_tip
        self._max_records = max_records
        self._max_bytes = max_bytes

        self._chain_tip = ""
        self._next_seq = 0
        self._segment_first_seq = 0
        self._active_count = 0
        self._pending_recovery: Path | None = None

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self._path, os.O_RDWR | os.O_APPEND | os.O_CREAT, self._FILE_MODE)
        if sys.platform != "win32":
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(self._fd)
                raise RuntimeError(
                    f"WormSink: another writer holds {self._path} (single-writer invariant)"
                ) from exc
            os.fchmod(self._fd, self._FILE_MODE)
        self._restore_tip()
        if self._pending_recovery is not None:
            # A torn tail was truncated during restore; record the recovery
            # explicitly through the normal append path so it gets the next seq.
            self._append(self._recovery_event(self._pending_recovery))
            self._pending_recovery = None

    @property
    def chain_tip(self) -> str:
        """SHA-256 hex of the most recently written record's ``event_hash``."""
        return self._chain_tip

    def close(self) -> None:
        """Release the lock and close the file descriptor."""
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __del__(self) -> None:
        with contextlib.suppress(Exception):  # reason: never raise from a finaliser
            self.close()

    # -- write path --------------------------------------------------------

    def write(self, event: AuditEvent) -> None:
        """Append one signed, chained record. Fail-open (AU-5)."""
        try:
            self._append(event)
        except Exception:  # reason: fail-open — auditing must never break the call (AU-5)
            _logger.warning("WormSink.write failed — swallowing (AU-5)", exc_info=True)

    def _append(self, event: AuditEvent) -> None:
        seq = self._next_seq
        prev_hash = self._chain_tip or self._genesis_tip
        event_dump = event.model_dump(mode="json")
        event_hash = _canonical_event_hash(seq=seq, prev_hash=prev_hash, event=event_dump)
        signature = self._signer.sign(event_hash.encode("utf-8")).hex()
        record = {
            "seq": seq,
            "event": event_dump,
            "prev_hash": prev_hash,
            "event_hash": event_hash,
            "algorithm": self._algorithm,
            "signature": signature,
        }
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        os.write(self._fd, line.encode("utf-8"))
        self._chain_tip = event_hash
        self._next_seq = seq + 1
        self._active_count += 1
        self._maybe_rotate()

    def _maybe_rotate(self) -> None:
        if self._active_count < self._max_records and os.fstat(self._fd).st_size < self._max_bytes:
            return
        segment = self._path.with_name(
            f"{self._path.stem}.{self._segment_first_seq:012d}{self._path.suffix}"
        )
        os.close(self._fd)
        self._path.rename(segment)
        self._fd = os.open(self._path, os.O_RDWR | os.O_APPEND | os.O_CREAT, self._FILE_MODE)
        if sys.platform != "win32":
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(self._fd, self._FILE_MODE)
        self._segment_first_seq = self._next_seq
        self._active_count = 0

    # -- startup recovery --------------------------------------------------

    def _restore_tip(self) -> None:
        """Restore tip/seq from existing segments + active file; recover torn tail."""
        last: dict[str, Any] | None = None
        active_records = 0
        for path in self._chain_files():
            is_active = path == self._path
            for record in self._iter_file(path, recover_torn=is_active):
                last = record
                if is_active:
                    active_records += 1
        if last is None:
            return
        self._chain_tip = last["event_hash"]
        self._next_seq = int(last["seq"]) + 1
        self._active_count = active_records
        # The first record currently in the active file started a segment.
        self._segment_first_seq = self._next_seq - active_records

    def _iter_file(self, path: Path, *, recover_torn: bool) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        raw = path.read_bytes()
        if not raw:
            return records
        lines = raw.split(b"\n")
        trailing_torn = raw[-1:] != b"\n"  # last element is a partial line, no newline
        for idx, chunk in enumerate(lines):
            if not chunk:
                continue
            try:
                records.append(json.loads(chunk))
            except json.JSONDecodeError:
                is_last = idx == len(lines) - 1
                if recover_torn and is_last and trailing_torn:
                    self._truncate_torn(path, raw, chunk)
                    self._pending_recovery = path
                    break
                _logger.warning("WormSink: skipping unparseable line in %s", path)
        return records

    def _truncate_torn(self, path: Path, raw: bytes, torn: bytes) -> None:
        """Drop a torn final line in place; the recovery marker is appended later."""
        with path.open("wb") as fh:
            fh.write(raw[: len(raw) - len(torn)])
        _logger.warning("WormSink: truncated torn final line in %s; recovery pending", path)

    def _recovery_event(self, path: Path) -> AuditEvent:
        return AuditEvent(
            actor_did="did:arc:arctrust:worm",
            action=self._RECOVERY_ACTION,
            target=path.name,
            outcome="recovered",
        )

    # -- verify path -------------------------------------------------------

    def verify_chain(self, public_key: bytes | None = None) -> bool:
        """Self-check this chain. Delegates to the lock-free module verifier."""
        pub = public_key if public_key is not None else self._public_key
        return verify_chain(self._path, pub, genesis_tip=self._genesis_tip)

    def _chain_files(self) -> list[Path]:
        return _segment_files(self._path)


# ---------------------------------------------------------------------------
# Lock-free chain verification (the `arc store verify` read path)
# ---------------------------------------------------------------------------


def _segment_files(path: Path) -> list[Path]:
    """Rotated segments (seq-ordered) followed by the active file."""
    segments = sorted(
        path.parent.glob(f"{path.stem}.*{path.suffix}"),
        key=lambda p: p.name,
    )
    return [*segments, path]


def _iter_records(path: Path) -> list[dict[str, Any]]:
    """Parse a chain file read-only, skipping any unparseable line."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for chunk in path.read_bytes().split(b"\n"):
        if not chunk:
            continue
        try:
            records.append(json.loads(chunk))
        except json.JSONDecodeError:
            _logger.warning("verify_chain: skipping unparseable line in %s", path)
    return records


def verify_chain(
    path: Path,
    public_key: bytes,
    *,
    genesis_tip: str = GENESIS_PREV_HASH,
) -> bool:
    """Validate a durable WORM chain on disk — no write lock required.

    Streams records across all rotated segments + the active file (no
    all-in-RAM list), checking for each record:
    - ``event_hash`` recomputes from ``(seq, prev_hash, event)`` (AU-9 links),
    - the ``signature`` verifies against ``public_key`` under the record's
      ``algorithm`` (Ed25519 or ECDSA-P256; AU-10 non-repudiation),
    - ``seq`` is contiguous from 0 (no gap / mid-deletion),
    - ``prev_hash`` chains to the previous record's ``event_hash``,
    - the first record's ``prev_hash`` equals the expected genesis tip.

    Returns False on any violation. This is the read path used by
    ``arc store verify`` and by store-ingest tamper flagging. Records written
    before SPEC-037 carry no ``algorithm`` field and are Ed25519 by definition,
    so the field defaults to ``ed25519`` (existing chains verify unchanged).
    """
    prev_hash = genesis_tip
    expected_seq = 0
    for segment in _segment_files(Path(path)):
        for record in _iter_records(segment):
            event_hash = _canonical_event_hash(
                seq=record["seq"], prev_hash=record["prev_hash"], event=record["event"]
            )
            if record.get("event_hash") != event_hash:
                return False
            if record.get("prev_hash") != prev_hash:
                return False
            if int(record.get("seq", -1)) != expected_seq:
                return False
            try:
                sig = bytes.fromhex(record.get("signature", ""))
            except ValueError:
                return False
            algorithm = record.get("algorithm", ED25519)
            if not verify_signature(algorithm, event_hash.encode("utf-8"), sig, public_key):
                return False
            prev_hash = event_hash
            expected_seq += 1
    return True


def read_verified_anchor(
    chain_path: Path,
    public_key: bytes,
    *,
    action: str = "trace.checkpoint",
    genesis_tip: str = GENESIS_PREV_HASH,
) -> dict[str, Any] | None:
    """Read the newest verified checkpoint anchor from a WORM chain.

    Checkpoints are ordinary ``AuditEvent`` records with ``action=action``
    (default ``"trace.checkpoint"``) and the checkpoint manifest carried in
    ``extra`` — no new schema. Lock-free and read-only, matching
    :func:`verify_chain`'s streaming style.

    1. If the chain itself fails :func:`verify_chain` (tampered, forged,
       gapped, or absent), return ``None`` — a chain that cannot attest to
       its own integrity cannot attest to anything it carries.
    2. Otherwise scan every record and return the ``extra`` dict of the
       LATEST record whose ``event.action == action``. Return ``None`` if
       no such record exists.

    This is the read half of the trace-checkpoint signed anchor: a caller
    combines the returned checkpoint's ``head_hash`` with a live trace
    store to prove the store was not rolled back past the last anchor
    (see ``arcllm.trace_retention.verify_against_anchor``).
    """
    if not verify_chain(chain_path, public_key, genesis_tip=genesis_tip):
        return None
    latest: dict[str, Any] | None = None
    for segment in _segment_files(Path(chain_path)):
        for record in _iter_records(segment):
            event = record.get("event", {})
            if event.get("action") == action:
                latest = event.get("extra")
    return latest


# ---------------------------------------------------------------------------
# emit() — safe dispatch to any sink
# ---------------------------------------------------------------------------


def emit(event: AuditEvent, sink: AuditSink) -> None:
    """Emit an audit event to a sink, swallowing all sink errors.

    Per NIST AU-5 (Response to Audit Processing Failures): the audit system
    must never interrupt the operation being audited. Sink failures are
    logged at WARNING but never re-raised.

    Args:
        event: The AuditEvent to emit.
        sink: Any object with a ``write(event) -> None`` method.
    """
    try:
        sink.write(event)
    except Exception:  # reason: fail-open — log + continue
        _logger.warning(
            "Audit sink %r raised on event %r — swallowing (AU-5)",
            type(sink).__name__,
            event.action,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Policy pipeline -> durable WORM adapter (SPEC-034 REQ-017)
# ---------------------------------------------------------------------------


def worm_policy_sink(sink: AuditSink) -> Callable[[str, dict[str, Any]], None]:
    """Adapt the policy pipeline's ``(event_type, payload)`` callback to a sink.

    The pipeline emits a flat ``(event_type, payload)`` audit callback once per
    evaluation; the durable :class:`WormSink` (and every :class:`AuditSink`)
    consumes an :class:`AuditEvent`. This closes that seam: it builds an
    ``AuditEvent`` from the payload and routes it through :func:`emit`, so a
    policy decision lands as one tamper-evident, Ed25519-signed chain record.

    Raw tool ``arguments`` are never copied — only the pipeline's precomputed
    ``input_hash`` travels into the record (REQ-019, AU-9 minimization). Lives
    in arctrust because arctrust owns both ``policy`` and ``audit``, so the
    adapter has no cross-package dependency.
    """

    def _write(event_type: str, payload: dict[str, Any]) -> None:
        event = AuditEvent(
            actor_did=str(payload.get("agent_did", "")),
            action=event_type,
            target=str(payload.get("tool_name", "")),
            outcome=str(payload.get("decision", "")),
            classification=payload.get("classification"),
            tier=payload.get("tier"),
            request_id=payload.get("session_id"),
            payload_hash=payload.get("input_hash"),
            extra={
                "layer": payload.get("layer"),
                "rule_id": payload.get("rule_id"),
                "reason": payload.get("reason"),
                "policy_version": payload.get("policy_version"),
                "cache_hit": payload.get("cache_hit"),
                "shadow": payload.get("shadow"),
                "evaluation_time_us": payload.get("evaluation_time_us"),
            },
        )
        emit(event, sink)

    return _write


__all__ = [
    "GENESIS_PREV_HASH",
    "AuditEvent",
    "AuditSink",
    "NullSink",
    "WormSink",
    "emit",
    "read_verified_anchor",
    "verify_chain",
    "worm_policy_sink",
]
