"""App principals: non-human callers that act through the ArcUI API (SPEC-087).

Models section (COMP-001). A :class:`Principal` is an app identity with its own
Ed25519 key and DID, a closed set of :class:`Scope` grants each bound to named
agents, and an allowlist of people it may act for. A :class:`Delegation` is a
person's time-boxed consent for an app to make decisions on their behalf.

Tier rules (:func:`validate_for_tier`) are stringency, not a gate: every tier
refuses unknown scopes and a DID not bound to the app's key; enterprise and
federal additionally refuse wildcards and unbounded expiry.

Every refusal is a :class:`PrincipalValidationError` whose ``reason`` is a
stable machine code that never echoes caller-supplied input.

Store section (COMP-003). :class:`PrincipalStore` keeps every principal and
delegation in one sealed, anchored authority record (``principals``) on
:class:`~arctrust.sealed_state.SealedAuthorityState`. Each mutation first
appends a durable ``principal.<verb>`` audit event attributed to the actor DID
the store was bound to, and is refused with no state change when that append
fails.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal

from arctrust.audit import AuditEvent, AuditSink, DurableAuditSink
from arctrust.byte_cipher import ByteCipher
from arctrust.canonical import canonical_json
from arctrust.identity import did_from_public_key, did_matches_pubkey, parse_did
from arctrust.monotonic import MonotonicAnchor
from arctrust.sealed_state import (
    AuthorityCommitUncertainError,
    SealedAuthorityState,
    UserStoreError,
)

__all__ = [
    "DECISION_SCOPES",
    "READ_SCOPES",
    "WILDCARD",
    "Delegation",
    "Principal",
    "PrincipalStatus",
    "PrincipalStore",
    "PrincipalStoreError",
    "PrincipalValidationError",
    "Scope",
    "Tier",
    "derive_app_did",
    "parse_scope",
    "validate_for_tier",
]

APP_AGENT_TYPE = "app"
WILDCARD = "*"
_ED25519_KEY_BYTES = 32
_CONTENT_HASH_DOMAIN = "arc.principal.v1"
_MALFORMED = "malformed_record"

Tier = Literal["personal", "enterprise", "federal"]
PrincipalStatus = Literal["pending", "active", "revoked"]

# Longest lifetime a principal may be issued for; personal is unbounded.
_MAX_LIFETIME: dict[str, timedelta | None] = {
    "personal": None,
    "enterprise": timedelta(days=90),
    "federal": timedelta(days=30),
}


class Scope(StrEnum):
    """The closed catalogue of things an app principal may be granted."""

    RUNS_START = "runs:start"
    RUNS_READ = "runs:read"
    WORKFLOWS_RUN = "workflows:run"
    WORKFLOWS_EVENT = "workflows:event"
    GATES_RESOLVE = "gates:resolve"
    TASKS_WRITE = "tasks:write"
    INBOX_SEND = "inbox:send"
    MEMORY_WRITE = "memory:write"
    FEEDBACK_WRITE = "feedback:write"
    EVENTS_SUBSCRIBE = "events:subscribe"


# Scopes that record a human decision; federal requires a live delegation.
DECISION_SCOPES: frozenset[Scope] = frozenset({Scope.GATES_RESOLVE, Scope.TASKS_WRITE})
# The only scopes a viewer session or the viewer token passes.
READ_SCOPES: frozenset[Scope] = frozenset({Scope.RUNS_READ, Scope.EVENTS_SUBSCRIBE})


class PrincipalValidationError(ValueError):
    """A principal or delegation was refused; ``reason`` is a stable code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_scope(value: object) -> Scope:
    """Return the catalogue member for ``value`` or refuse with ``unknown_scope``."""
    if not isinstance(value, str):
        raise PrincipalValidationError("unknown_scope")
    try:
        return Scope(value)
    except ValueError:
        raise PrincipalValidationError("unknown_scope") from None


def derive_app_did(public_key: bytes, *, org: str) -> str:
    """Derive the app DID bound to ``public_key`` (agent_type ``app``)."""
    return did_from_public_key(public_key, org=org, agent_type=APP_AGENT_TYPE)


@dataclass(frozen=True)
class Principal:
    """An app identity: key-bound DID, scoped agent grants, subject allowlist.

    ``scopes`` maps each granted scope to the agent DIDs it applies to;
    ``subjects`` lists the people the app may act for. ``(WILDCARD,)`` in
    either place means "any" and is only valid at personal tier.
    """

    id: str
    name: str
    did: str
    public_key: str
    secret_digest: str
    scopes: Mapping[Scope, tuple[str, ...]]
    subjects: tuple[str, ...]
    status: PrincipalStatus
    created_by: str
    approved_by: str | None
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_by: str | None
    revoked_at: datetime | None

    def content_hash(self) -> str:
        """SHA-256 hex over the authority fields an approval row binds.

        Lifecycle fields (status, approver, last use, revocation) are excluded
        so approving or using a principal never changes the hash it was
        approved under. Scope and agent order do not matter.
        """
        authority = {
            "domain": _CONTENT_HASH_DOMAIN,
            "id": self.id,
            "name": self.name,
            "did": self.did,
            "public_key": self.public_key,
            "secret_digest": self.secret_digest,
            "scopes": {str(scope): sorted(agents) for scope, agents in self.scopes.items()},
            "subjects": sorted(self.subjects),
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "expires_at": _iso_or_none(self.expires_at),
        }
        return hashlib.sha256(canonical_json(authority)).hexdigest()

    def to_dict(self) -> dict[str, object]:
        """JSON-safe form for the sealed store; inverse of :meth:`from_dict`."""
        return {
            "id": self.id,
            "name": self.name,
            "did": self.did,
            "public_key": self.public_key,
            "secret_digest": self.secret_digest,
            "scopes": {str(scope): list(agents) for scope, agents in self.scopes.items()},
            "subjects": list(self.subjects),
            "status": self.status,
            "created_by": self.created_by,
            "approved_by": self.approved_by,
            "created_at": self.created_at.isoformat(),
            "expires_at": _iso_or_none(self.expires_at),
            "last_used_at": _iso_or_none(self.last_used_at),
            "revoked_by": self.revoked_by,
            "revoked_at": _iso_or_none(self.revoked_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Principal:
        """Rebuild a principal from stored JSON; any malformed field fails closed."""
        record = _as_record(data)
        return cls(
            id=_req_str(record, "id"),
            name=_req_str(record, "name"),
            did=_req_str(record, "did"),
            public_key=_req_str(record, "public_key"),
            secret_digest=_req_str(record, "secret_digest"),
            scopes=_scope_grants(record.get("scopes")),
            subjects=_str_tuple(record.get("subjects")),
            status=_status(record.get("status")),
            created_by=_req_str(record, "created_by"),
            approved_by=_opt_str(record, "approved_by"),
            created_at=_req_time(record, "created_at"),
            expires_at=_opt_time(record, "expires_at"),
            last_used_at=_opt_time(record, "last_used_at"),
            revoked_by=_opt_str(record, "revoked_by"),
            revoked_at=_opt_time(record, "revoked_at"),
        )


@dataclass(frozen=True)
class Delegation:
    """A person's time-boxed consent for an app to decide on their behalf."""

    id: str
    person_did: str
    principal_did: str
    scopes: tuple[Scope, ...]
    created_at: datetime
    expires_at: datetime
    revoked: bool

    def to_dict(self) -> dict[str, object]:
        """JSON-safe form for the sealed store; inverse of :meth:`from_dict`."""
        return {
            "id": self.id,
            "person_did": self.person_did,
            "principal_did": self.principal_did,
            "scopes": [str(scope) for scope in self.scopes],
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Delegation:
        """Rebuild a delegation from stored JSON; any malformed field fails closed."""
        record = _as_record(data)
        revoked = record.get("revoked")
        if not isinstance(revoked, bool):
            raise PrincipalValidationError(_MALFORMED)
        return cls(
            id=_req_str(record, "id"),
            person_did=_req_str(record, "person_did"),
            principal_did=_req_str(record, "principal_did"),
            scopes=tuple(parse_scope(scope) for scope in _str_tuple(record.get("scopes"))),
            created_at=_req_time(record, "created_at"),
            expires_at=_req_time(record, "expires_at"),
            revoked=revoked,
        )


def validate_for_tier(principal: Principal, tier: Tier, *, now: datetime) -> None:
    """Refuse ``principal`` if it breaks the rules for ``tier``.

    An unrecognised tier fails closed rather than falling back to personal.
    """
    if tier not in _MAX_LIFETIME:
        raise PrincipalValidationError("unknown_tier")
    for scope in principal.scopes:
        parse_scope(scope)
    _check_did_binding(principal)
    if tier == "personal":
        return
    _check_no_wildcards(principal)
    _check_expiry(principal.expires_at, _MAX_LIFETIME[tier], now=now)


def _check_did_binding(principal: Principal) -> None:
    """The DID must be an ``app`` DID derived from the principal's own key."""
    try:
        key = bytes.fromhex(principal.public_key)
        agent_type = parse_did(principal.did)["agent_type"]
    except ValueError:
        raise PrincipalValidationError("did_mismatch") from None
    if len(key) != _ED25519_KEY_BYTES or agent_type != APP_AGENT_TYPE:
        raise PrincipalValidationError("did_mismatch")
    if not did_matches_pubkey(principal.did, key):
        raise PrincipalValidationError("did_mismatch")


def _check_no_wildcards(principal: Principal) -> None:
    if any(WILDCARD in agents for agents in principal.scopes.values()):
        raise PrincipalValidationError("wildcard_agent")
    if WILDCARD in principal.subjects:
        raise PrincipalValidationError("wildcard_subject")


def _check_expiry(
    expires_at: datetime | None, max_lifetime: timedelta | None, *, now: datetime
) -> None:
    if max_lifetime is None:
        return
    if expires_at is None:
        raise PrincipalValidationError("missing_expiry")
    if expires_at - now > max_lifetime:
        raise PrincipalValidationError("expiry_too_long")


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


# ---------------------------------------------------------------------------
# Stored-record decoding: strict, fail-closed, never echoes the bad value
# ---------------------------------------------------------------------------


def _as_record(data: object) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise PrincipalValidationError(_MALFORMED)
    return data


def _req_str(record: Mapping[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise PrincipalValidationError(_MALFORMED)
    return value


def _opt_str(record: Mapping[str, object], key: str) -> str | None:
    return None if record.get(key) is None else _req_str(record, key)


def _req_time(record: Mapping[str, object], key: str) -> datetime:
    try:
        moment = datetime.fromisoformat(_req_str(record, key))
    except ValueError:
        raise PrincipalValidationError(_MALFORMED) from None
    if moment.tzinfo is None:
        raise PrincipalValidationError(_MALFORMED)
    return moment


def _opt_time(record: Mapping[str, object], key: str) -> datetime | None:
    return None if record.get(key) is None else _req_time(record, key)


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PrincipalValidationError(_MALFORMED)
    return tuple(value)


def _scope_grants(value: object) -> dict[Scope, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise PrincipalValidationError(_MALFORMED)
    return {parse_scope(scope): _str_tuple(agents) for scope, agents in value.items()}


def _status(value: object) -> PrincipalStatus:
    match value:
        case "pending" | "active" | "revoked":
            return value
        case _:
            raise PrincipalValidationError(_MALFORMED)


# ---------------------------------------------------------------------------
# Store (COMP-003): one sealed record holds principals and delegations
# ---------------------------------------------------------------------------

_RECORD_NAME = "principals"
_LOCK_TIMEOUT_SECONDS = 5
# last_used_at is bookkeeping, not authority: write it at most once a minute.
_TOUCH_INTERVAL = timedelta(minutes=1)

_Principals = dict[str, Principal]
_Delegations = dict[str, Delegation]


class PrincipalStoreError(UserStoreError):
    """A principal or delegation mutation was refused, or the record is malformed."""


class PrincipalStore:
    """Load/save app principals and delegations. Every mutation writes through.

    ``actor_did`` is bound by :class:`~arctrust.authority.AccountAuthority`
    after it authenticates the caller; it is the actor on every audit event.
    DIDs passed to individual methods (approver, revoker) are recorded as
    record fields and audit detail, never as the audit actor.
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
    ) -> None:
        self.path = path
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
        self._principals: _Principals = {}
        self._delegations: _Delegations = {}
        # Nothing is read here: every query and mutation loads the anchored head
        # itself, so integrity and rollback failures surface on each use.
        self._mutex = threading.RLock()

    # --- queries --------------------------------------------------------

    def get(self, principal_id: str) -> Principal | None:
        with self._serialized():
            self._load()
            return self._principals.get(principal_id)

    def get_by_did(self, did: str) -> Principal | None:
        with self._serialized():
            self._load()
            return self._find_by_did(did)

    def delegations_for(self, person_did: str) -> list[Delegation]:
        with self._serialized():
            self._load()
            return [d for d in self._delegations.values() if d.person_did == person_did]

    def live_delegation(
        self, person_did: str, principal_did: str, scope: Scope, now: datetime
    ) -> Delegation | None:
        """The unrevoked delegation covering ``scope`` at ``now`` (created_at <= now < expiry)."""
        for delegation in self.delegations_for(person_did):
            if _is_live(delegation, principal_did, scope, now):
                return delegation
        return None

    # Defined after every signature that says ``list[...]``: inside the class
    # body the name would otherwise resolve to this method.
    def list(self) -> list[Principal]:
        with self._serialized():
            self._load()
            return sorted(self._principals.values(), key=lambda p: (p.created_at, p.id))

    # --- principal mutations --------------------------------------------

    def request(self, principal: Principal) -> Principal:
        """Store a new principal as ``pending``; it is unusable until activated."""
        with self._serialized():
            self._load()
            self._check_requestable(principal)
            self._commit("request", principal, {**self._principals, principal.id: principal})
            return principal

    def activate(self, principal_id: str, approver_did: str, expected_hash: str) -> Principal:
        """Activate a pending principal whose authority fields match ``expected_hash``."""
        with self._serialized():
            self._load()
            pending = self._require_principal("activate", principal_id)
            if pending.status != "pending":
                raise self._refuse("activate", principal_id, "not_pending")
            if not hmac.compare_digest(pending.content_hash(), expected_hash):
                raise self._refuse("activate", principal_id, "content_hash_mismatch")
            active = replace(pending, status="active", approved_by=approver_did)
            self._commit(
                "activate",
                active,
                {**self._principals, active.id: active},
                detail={"approver_did": approver_did},
            )
            return active

    def revoke(self, principal_id: str, actor_did: str) -> Principal:
        """Revoke a principal; a revoked principal never authenticates again."""
        with self._serialized():
            self._load()
            current = self._require_principal("revoke", principal_id)
            if current.status == "revoked":
                raise self._refuse("revoke", principal_id, "already_revoked")
            revoked = replace(
                current, status="revoked", revoked_by=actor_did, revoked_at=datetime.now(UTC)
            )
            self._commit(
                "revoke",
                revoked,
                {**self._principals, revoked.id: revoked},
                detail={"revoked_by": actor_did},
            )
            return revoked

    def touch(self, principal_id: str, now: datetime) -> None:
        """Record use; skipped while the last recorded use is under a minute old."""
        with self._serialized():
            self._load()
            current = self._principals.get(principal_id)
            if current is None:
                raise PrincipalStoreError("unknown principal")
            last = current.last_used_at
            if last is not None and now - last < _TOUCH_INTERVAL:
                return
            touched = replace(current, last_used_at=now)
            self._save({**self._principals, touched.id: touched}, self._delegations)

    # --- delegation mutations -------------------------------------------

    def add_delegation(self, delegation: Delegation) -> Delegation:
        """Store a person's delegation to a known, unrevoked principal."""
        with self._serialized():
            self._load()
            self._check_delegable(delegation)
            self._commit_delegation(
                "delegation.add", delegation, {**self._delegations, delegation.id: delegation}
            )
            return delegation

    def revoke_delegation(self, delegation_id: str, actor_did: str) -> Delegation:
        """Revoke a delegation; only the person who granted it may revoke it."""
        with self._serialized():
            self._load()
            current = self._delegations.get(delegation_id)
            if current is None:
                raise self._refuse("delegation.revoke", delegation_id, "unknown_delegation")
            if current.person_did != actor_did:
                raise self._refuse("delegation.revoke", delegation_id, "not_owner")
            if current.revoked:
                raise self._refuse("delegation.revoke", delegation_id, "already_revoked")
            revoked = replace(current, revoked=True)
            self._commit_delegation(
                "delegation.revoke", revoked, {**self._delegations, revoked.id: revoked}
            )
            return revoked

    # --- checks ---------------------------------------------------------

    def _check_requestable(self, principal: Principal) -> None:
        if principal.status != "pending" or principal.approved_by is not None:
            raise self._refuse("request", principal.id, "not_pending")
        try:
            # Tier-independent rules only (catalogue scopes, DID bound to key);
            # tier stringency belongs to the issuing route.
            validate_for_tier(principal, "personal", now=principal.created_at)
        except PrincipalValidationError as exc:
            raise self._refuse("request", principal.id, exc.reason) from None
        if principal.id in self._principals:
            raise self._refuse("request", principal.id, "duplicate_id")
        if self._find_by_did(principal.did) is not None:
            raise self._refuse("request", principal.id, "duplicate_did")

    def _check_delegable(self, delegation: Delegation) -> None:
        if delegation.id in self._delegations:
            raise self._refuse("delegation.add", delegation.id, "duplicate_id")
        principal = self._find_by_did(delegation.principal_did)
        if principal is None or principal.status == "revoked":
            raise self._refuse("delegation.add", delegation.id, "unknown_principal")

    def _require_principal(self, verb: str, principal_id: str) -> Principal:
        principal = self._principals.get(principal_id)
        if principal is None:
            raise self._refuse(verb, principal_id, "unknown_principal")
        return principal

    def _find_by_did(self, did: str) -> Principal | None:
        return next((p for p in self._principals.values() if p.did == did), None)

    # --- audit + persistence --------------------------------------------

    def _commit(
        self,
        verb: str,
        principal: Principal,
        candidate: _Principals,
        *,
        detail: Mapping[str, str] | None = None,
    ) -> None:
        extra = {"principal_did": principal.did, **(detail or {})}
        self._audit_durable(verb, principal.id, principal.content_hash(), extra)
        self._save(candidate, self._delegations)

    def _commit_delegation(
        self, verb: str, delegation: Delegation, candidate: _Delegations
    ) -> None:
        extra = {
            "principal_did": delegation.principal_did,
            "person_did": delegation.person_did,
            "scopes": ",".join(sorted(str(scope) for scope in delegation.scopes)),
        }
        self._audit_durable(verb, delegation.id, None, extra)
        self._save(self._principals, candidate)

    def _audit_durable(
        self, verb: str, target: str, payload_hash: str | None, extra: Mapping[str, str]
    ) -> None:
        # Before the commit: a failed durable append raises and nothing is saved.
        self._strict_audit_sink.write_durable(
            AuditEvent(
                actor_did=self._actor_did,
                action=f"principal.{verb}",
                target=target,
                outcome="allow",
                payload_hash=payload_hash,
                extra=dict(extra),
            )
        )

    def _refuse(self, verb: str, target: str, reason: str) -> PrincipalStoreError:
        self._audit_sink.write(
            AuditEvent(
                actor_did=self._actor_did,
                action=f"principal.{verb}",
                target=target,
                outcome="deny",
                extra={"reason": reason},
            )
        )
        return PrincipalStoreError(f"principal {verb} refused: {reason}")

    def _load(self) -> None:
        decoded: list[tuple[_Principals, _Delegations]] = []
        self._state.load(validate=lambda record: decoded.append(_decode_record(record)))
        self._principals, self._delegations = decoded[0] if decoded else ({}, {})

    def _save(self, principals: _Principals, delegations: _Delegations) -> None:
        payload: dict[str, object] = {
            "principals": [p.to_dict() for p in principals.values()],
            "delegations": [d.to_dict() for d in delegations.values()],
        }
        try:
            self._state.save(payload)
        except AuthorityCommitUncertainError:
            # Anchored and written: the candidate is the authority now.
            self._principals, self._delegations = principals, delegations
            raise
        self._principals, self._delegations = principals, delegations

    @contextmanager
    def _serialized(self) -> Iterator[None]:
        # Load-check-save must not interleave between threads sharing one store.
        if not self._mutex.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            raise PrincipalStoreError("principal authority instance is busy")
        try:
            yield
        finally:
            self._mutex.release()


def _is_live(delegation: Delegation, principal_did: str, scope: Scope, now: datetime) -> bool:
    return (
        not delegation.revoked
        and delegation.principal_did == principal_did
        and scope in delegation.scopes
        and delegation.created_at <= now < delegation.expires_at
    )


def _decode_record(raw: Mapping[str, object]) -> tuple[_Principals, _Delegations]:
    """Decode the whole anchored record or refuse it; duplicate ids or DIDs are malformed."""
    try:
        principals = [Principal.from_dict(item) for item in _record_list(raw, "principals")]
        delegations = [Delegation.from_dict(item) for item in _record_list(raw, "delegations")]
    except PrincipalValidationError as exc:
        raise PrincipalStoreError("malformed principal authority record") from exc
    by_id = {p.id: p for p in principals}
    delegations_by_id = {d.id: d for d in delegations}
    if len(by_id) != len(principals) or len({p.did for p in principals}) != len(principals):
        raise PrincipalStoreError("malformed principal authority record")
    if len(delegations_by_id) != len(delegations):
        raise PrincipalStoreError("malformed principal authority record")
    return by_id, delegations_by_id


def _record_list(raw: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    items = raw.get(key)
    if not isinstance(items, list):
        raise PrincipalValidationError(_MALFORMED)
    return [_as_record(item) for item in items]
