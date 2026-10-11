"""Callback allowlist: the origins work-event callbacks may be sent to (SPEC-088 COMP-018).

Models section. An :class:`AllowlistEntry` names one ``scheme://host[:port]``
origin, whether it may resolve to a private address, and the highest
classification it is cleared to receive. :func:`normalize_origin` is the one
place an operator-supplied origin becomes the canonical form an exact match
compares against.

Store section. :class:`CallbackAllowlistStore` keeps every entry in one sealed,
anchored authority record (``callbacks``) on
:class:`~arctrust.sealed_state.SealedAuthorityState`, the same seam as the
``principals`` record. A file edit, database write or restored backup cannot
add an origin or undo a removal: every query and mutation reloads the anchored
head, so a rolled-back record refuses on every use.

At personal tier an add is active at once. At enterprise and federal it is
``pending`` until :meth:`CallbackAllowlistStore.activate` is called with the
entry's content hash (the hash the ``work_event_callback`` approval row binds);
federal also refuses an approver who is the requester.

Each mutation first appends a durable ``work_event.callback_allowlist.<verb>``
audit event attributed to the actor DID the store was bound to, and is refused
with no state change when that append fails. Every refusal is a
:class:`CallbackAllowlistError` with a stable ``reason`` and a deny event.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urlsplit

from arctrust.audit import AuditEvent, AuditSink, DurableAuditSink
from arctrust.byte_cipher import ByteCipher
from arctrust.canonical import canonical_json
from arctrust.classification import Classification
from arctrust.monotonic import MonotonicAnchor
from arctrust.principals import Tier
from arctrust.sealed_state import (
    AuthorityCommitUncertainError,
    SealedAuthorityState,
    UserStoreError,
)

__all__ = [
    "CALLBACK_ALLOWLIST_TOOL",
    "AllowlistEntry",
    "CallbackAllowlistError",
    "CallbackAllowlistStore",
    "CallbackOriginError",
    "EntryStatus",
    "NewCallbackEntry",
    "normalize_origin",
]

#: ``PendingApproval.tool`` of an enterprise/federal callback-origin add request.
#: Approving such a row activates the entry it names; denying it removes it.
CALLBACK_ALLOWLIST_TOOL = "work_event_callback"

EntryStatus = Literal["pending", "active", "removed"]

_INVALID_ORIGIN = "invalid_origin"
_MALFORMED = "malformed_record"
_CONTENT_HASH_DOMAIN = "arc.callback_allowlist.v1"
_DEFAULT_PORTS = {"https": 443, "http": 80}
# DNS names only: letters, digits, hyphens, dots. Anything else is not an origin.
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_TIERS = frozenset(get_args(Tier))


class CallbackOriginError(ValueError):
    """An origin is not ``scheme://host[:port]``; ``reason`` is ``invalid_origin``."""

    def __init__(self) -> None:
        super().__init__(_INVALID_ORIGIN)
        self.reason = _INVALID_ORIGIN


class CallbackAllowlistError(UserStoreError):
    """A callback allowlist mutation was refused; ``reason`` is a stable code."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"callback allowlist refused: {reason}")
        self.reason = reason


# ---------------------------------------------------------------------------
# Origin normalization
# ---------------------------------------------------------------------------


def normalize_origin(value: str) -> str:
    """Return ``value`` as a canonical ``scheme://host[:port]`` origin.

    Scheme and host are lowercased; the scheme's default port is dropped. Any
    path (even ``/``), query, fragment, userinfo, scheme other than http/https,
    missing host or bad port is refused. The refusal never echoes the input.
    """
    if not isinstance(value, str):
        raise CallbackOriginError
    scheme, sep, netloc = value.partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in _DEFAULT_PORTS or any(ch in netloc for ch in "/?#@\\"):
        raise CallbackOriginError
    host, port = _split_netloc(netloc)
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _split_netloc(netloc: str) -> tuple[str, int | None]:
    """``host[:port]`` -> (canonical host, port or None); IPv6 keeps its brackets."""
    parts = urlsplit(f"//{netloc}")
    try:
        port = parts.port
    except ValueError:
        raise CallbackOriginError from None
    hostname = parts.hostname
    if not hostname or netloc.endswith(":") or port == 0:
        raise CallbackOriginError
    return _canonical_host(hostname, bracketed=netloc.startswith("[")), port


def _canonical_host(hostname: str, *, bracketed: bool) -> str:
    if bracketed:
        try:
            address = ipaddress.IPv6Address(hostname)
        except ValueError:
            raise CallbackOriginError from None
        if address.scope_id is not None:
            raise CallbackOriginError
        return f"[{address.compressed}]"
    if not _HOSTNAME_RE.fullmatch(hostname):
        raise CallbackOriginError
    return hostname


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewCallbackEntry:
    """An operator's request to allow callbacks to ``origin``."""

    origin: str
    allow_private: bool = False
    clearance: Classification = Classification.UNCLASSIFIED


@dataclass(frozen=True)
class AllowlistEntry:
    """One allowlisted callback origin and its lifecycle."""

    id: str
    origin: str
    allow_private: bool
    clearance: Classification
    status: EntryStatus
    created_by: str
    approved_by: str | None
    created_at: datetime

    def content_hash(self) -> str:
        """SHA-256 hex over the authority fields an approval row binds.

        Lifecycle fields (status, approver) are excluded so approving an entry
        never changes the hash it was approved under.
        """
        authority = {
            "domain": _CONTENT_HASH_DOMAIN,
            "id": self.id,
            "origin": self.origin,
            "allow_private": self.allow_private,
            "clearance": self.clearance.name,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
        }
        return hashlib.sha256(canonical_json(authority)).hexdigest()

    def to_dict(self) -> dict[str, object]:
        """JSON-safe form for the sealed store; inverse of :meth:`from_dict`."""
        return {
            "id": self.id,
            "origin": self.origin,
            "allow_private": self.allow_private,
            "clearance": self.clearance.name,
            "status": self.status,
            "created_by": self.created_by,
            "approved_by": self.approved_by,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> AllowlistEntry:
        """Rebuild an entry from stored JSON; any malformed field fails closed."""
        origin = _req_str(data, "origin")
        try:
            if normalize_origin(origin) != origin:
                raise CallbackAllowlistError(_MALFORMED)
        except CallbackOriginError:
            raise CallbackAllowlistError(_MALFORMED) from None
        allow_private = data.get("allow_private")
        if not isinstance(allow_private, bool):
            raise CallbackAllowlistError(_MALFORMED)
        return cls(
            id=_req_str(data, "id"),
            origin=origin,
            allow_private=allow_private,
            clearance=_clearance(data.get("clearance")),
            status=_status(data.get("status")),
            created_by=_req_str(data, "created_by"),
            approved_by=_opt_str(data, "approved_by"),
            created_at=_req_time(data, "created_at"),
        )


# ---------------------------------------------------------------------------
# Stored-record decoding: strict, fail-closed, never echoes the bad value
# ---------------------------------------------------------------------------


def _req_str(record: Mapping[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise CallbackAllowlistError(_MALFORMED)
    return value


def _opt_str(record: Mapping[str, object], key: str) -> str | None:
    return None if record.get(key) is None else _req_str(record, key)


def _req_time(record: Mapping[str, object], key: str) -> datetime:
    try:
        moment = datetime.fromisoformat(_req_str(record, key))
    except ValueError:
        raise CallbackAllowlistError(_MALFORMED) from None
    if moment.tzinfo is None:
        raise CallbackAllowlistError(_MALFORMED)
    return moment


def _clearance(value: object) -> Classification:
    if not isinstance(value, str) or value not in Classification.__members__:
        raise CallbackAllowlistError(_MALFORMED)
    return Classification[value]


def _status(value: object) -> EntryStatus:
    match value:
        case "pending" | "active" | "removed":
            return value
        case _:
            raise CallbackAllowlistError(_MALFORMED)


def _decode_record(raw: Mapping[str, object]) -> dict[str, AllowlistEntry]:
    """Decode the anchored record or refuse it; duplicate ids or live origins are malformed."""
    items = raw.get("entries")
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise CallbackAllowlistError(_MALFORMED)
    entries = [AllowlistEntry.from_dict(item) for item in items]
    by_id = {entry.id: entry for entry in entries}
    live = [entry.origin for entry in entries if entry.status != "removed"]
    if len(by_id) != len(entries) or len(set(live)) != len(live):
        raise CallbackAllowlistError(_MALFORMED)
    return by_id


# ---------------------------------------------------------------------------
# Store: one sealed record holds every entry
# ---------------------------------------------------------------------------

_RECORD_NAME = "callbacks"
_ACTION_PREFIX = "work_event.callback_allowlist"
_LOCK_TIMEOUT_SECONDS = 5

_Entries = dict[str, AllowlistEntry]


class CallbackAllowlistStore:
    """Load/save callback allowlist entries. Every mutation writes through.

    ``actor_did`` is bound by :class:`~arctrust.authority.AccountAuthority`
    after it authenticates the caller; it is the actor on every audit event.
    DIDs passed to individual methods (requester, approver, remover) are
    recorded as record fields and audit detail, never as the audit actor.
    """

    def __init__(
        self,
        path: Path,
        *,
        anchor: MonotonicAnchor,
        cipher: ByteCipher,
        audit_sink: AuditSink,
        strict_audit_sink: DurableAuditSink,
        actor_did: str,
        tier: Tier,
    ) -> None:
        if tier not in _TIERS:
            # An unrecognised tier fails closed rather than falling back to personal.
            raise CallbackAllowlistError("unknown_tier")
        self.path = path
        self._tier = tier
        self._audit_sink = audit_sink
        self._strict_audit_sink = strict_audit_sink
        self._actor_did = actor_did
        self._state = SealedAuthorityState(
            path,
            anchor=anchor,
            cipher=cipher,
            audit_sink=audit_sink,
            strict_audit_sink=strict_audit_sink,
            record_name=_RECORD_NAME,
            actor_did=actor_did,
        )
        self._entries: _Entries = {}
        # Nothing is read here: every query and mutation loads the anchored head
        # itself, so integrity and rollback failures surface on each use.
        self._mutex = threading.RLock()

    # --- mutations ------------------------------------------------------

    def request(self, entry: NewCallbackEntry, actor_did: str) -> AllowlistEntry:
        """Add ``entry``: active at once at personal tier, else pending approval."""
        with self._serialized():
            self._load()
            entry_id = uuid.uuid4().hex
            try:
                origin = normalize_origin(entry.origin)
            except CallbackOriginError:
                raise self._refuse("request", entry_id, _INVALID_ORIGIN) from None
            if any(e.origin == origin and e.status != "removed" for e in self._entries.values()):
                raise self._refuse("request", entry_id, "duplicate_origin")
            personal = self._tier == "personal"
            created = AllowlistEntry(
                id=entry_id,
                origin=origin,
                allow_private=entry.allow_private,
                clearance=entry.clearance,
                status="active" if personal else "pending",
                created_by=actor_did,
                approved_by=actor_did if personal else None,
                created_at=datetime.now(UTC),
            )
            self._commit("request", created, {"requested_by": actor_did})
            return created

    def activate(self, entry_id: str, approver_did: str, expected_hash: str) -> AllowlistEntry:
        """Activate a pending entry whose authority fields match ``expected_hash``."""
        with self._serialized():
            self._load()
            pending = self._require("activate", entry_id)
            if pending.status != "pending":
                raise self._refuse("activate", entry_id, "not_pending")
            if not hmac.compare_digest(pending.content_hash(), expected_hash):
                raise self._refuse("activate", entry_id, "content_hash_mismatch")
            if self._tier == "federal" and approver_did == pending.created_by:
                raise self._refuse("activate", entry_id, "second_operator_required")
            active = replace(pending, status="active", approved_by=approver_did)
            self._commit("activate", active, {"approver_did": approver_did})
            return active

    def remove(self, entry_id: str, actor_did: str) -> AllowlistEntry:
        """Remove an entry; callbacks to its origin are refused from the next check."""
        with self._serialized():
            self._load()
            current = self._require("remove", entry_id)
            if current.status == "removed":
                raise self._refuse("remove", entry_id, "already_removed")
            removed = replace(current, status="removed")
            self._commit("remove", removed, {"removed_by": actor_did})
            return removed

    # --- audit + persistence --------------------------------------------

    def _require(self, verb: str, entry_id: str) -> AllowlistEntry:
        entry = self._entries.get(entry_id)
        if entry is None:
            raise self._refuse(verb, entry_id, "unknown_entry")
        return entry

    def _commit(self, verb: str, entry: AllowlistEntry, detail: Mapping[str, str]) -> None:
        # Before the save: a failed durable append raises and nothing is written.
        self._strict_audit_sink.write_durable(
            AuditEvent(
                actor_did=self._actor_did,
                action=f"{_ACTION_PREFIX}.{verb}",
                target=entry.id,
                outcome="allow",
                tier=self._tier,
                payload_hash=entry.content_hash(),
                extra={"origin": entry.origin, "status": entry.status, **detail},
            )
        )
        self._save({**self._entries, entry.id: entry})

    def _refuse(self, verb: str, target: str, reason: str) -> CallbackAllowlistError:
        self._audit_sink.write(
            AuditEvent(
                actor_did=self._actor_did,
                action=f"{_ACTION_PREFIX}.{verb}",
                target=target,
                outcome="deny",
                tier=self._tier,
                extra={"reason": reason},
            )
        )
        return CallbackAllowlistError(reason)

    def _load(self) -> None:
        decoded: list[_Entries] = []
        self._state.load(validate=lambda record: decoded.append(_decode_record(record)))
        self._entries = decoded[0] if decoded else {}

    def _save(self, entries: _Entries) -> None:
        payload: dict[str, object] = {"entries": [e.to_dict() for e in entries.values()]}
        try:
            self._state.save(payload)
        except AuthorityCommitUncertainError:
            # Anchored and written: the candidate is the authority now.
            self._entries = entries
            raise
        self._entries = entries

    @contextmanager
    def _serialized(self) -> Iterator[None]:
        # Load-check-save must not interleave between threads sharing one store.
        if not self._mutex.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            raise CallbackAllowlistError("busy")
        try:
            yield
        finally:
            self._mutex.release()

    # --- queries --------------------------------------------------------

    # Defined last: inside the class body ``list`` would otherwise resolve to
    # this method in any later ``list[...]`` annotation.
    def list(self) -> list[AllowlistEntry]:
        """Every entry, oldest first, read from the anchored head."""
        with self._serialized():
            self._load()
            return sorted(self._entries.values(), key=lambda e: (e.created_at, e.id))
