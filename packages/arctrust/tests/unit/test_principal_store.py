"""PrincipalStore: the sealed, anchored authority record for app principals (SPEC-087 COMP-003).

Real collaborators only: a ``FileJournalAnchor`` signed by an ``InProcessSigner`` is the
monotonic anchor and files live on the real filesystem. The cipher (Vault custody in
production) and the audit sinks are the external boundaries, so they are small doubles;
the cipher is a real authenticated NaCl SecretBox so tampering is detected, not simulated.

Covers REQ-549 (pending until approved), REQ-553 (approval binds the content hash),
REQ-556 (revocation survives reopen and rollback), REQ-564 (delegations honour expiry).
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from nacl.secret import SecretBox
from nacl.signing import SigningKey

from arctrust import FileJournalAnchor, InProcessSigner
from arctrust.audit import AuditEvent
from arctrust.principals import (
    Delegation,
    Principal,
    PrincipalStore,
    PrincipalStoreError,
    Scope,
    derive_app_did,
)
from arctrust.sealed_state import AuthorityStateError
from arctrust.users import UserStoreError

_ACTOR = "did:arc:test:operator/principal-store-actor"
_APPROVER = "did:arc:test:user/principal-approver"
_PERSON = "did:arc:test:user/person-ada"
_OTHER_PERSON = "did:arc:test:user/person-bob"
_AGENT = "did:arc:test:agent/olivia"
_OPERATOR = InProcessSigner(bytes(range(32)))
_NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
_MUTATION_ACTIONS = {
    "principal.request",
    "principal.activate",
    "principal.revoke",
    "principal.delegation.add",
    "principal.delegation.revoke",
}


# --- external-boundary doubles -------------------------------------------------


class SecretBoxCipher:
    """Authenticated ByteCipher standing in for Vault custody."""

    def __init__(self) -> None:
        self._box = SecretBox(bytes(range(32)))

    def seal(self, payload: bytes) -> str:
        return base64.b64encode(bytes(self._box.encrypt(payload))).decode("ascii")

    def open(self, sealed: str) -> bytes:
        return self._box.decrypt(base64.b64decode(sealed, validate=True))


@dataclass
class RecordingSink:
    events: list[AuditEvent] = field(default_factory=list)

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class StrictSink:
    """Durable audit chain double; refuses appends whose action matches ``fail_prefix``."""

    events: list[AuditEvent] = field(default_factory=list)
    fail_prefix: str | None = None

    def write_durable(self, event: AuditEvent) -> None:
        if self.fail_prefix is not None and event.action.startswith(self.fail_prefix):
            raise OSError("injected durable append failure")
        self.events.append(event)


# --- fixtures ------------------------------------------------------------------


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@dataclass
class Rig:
    path: Path
    anchor: FileJournalAnchor
    cipher: SecretBoxCipher
    audit: RecordingSink
    strict: StrictSink

    def open(self, actor_did: str = _ACTOR) -> PrincipalStore:
        return PrincipalStore(
            self.path,
            anchor=self.anchor,
            cipher=self.cipher,
            audit_sink=self.audit,
            strict_audit_sink=self.strict,
            actor_did=actor_did,
        )

    def all_events(self) -> list[AuditEvent]:
        return [*self.audit.events, *self.strict.events]

    def mutation_events(self) -> list[AuditEvent]:
        return [e for e in self.all_events() if e.action in _MUTATION_ACTIONS]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    state_dir = _private_dir(tmp_path / "state")
    return Rig(
        path=state_dir / "principals.json",
        anchor=FileJournalAnchor(
            _private_dir(tmp_path / "anchor"), scope="test/principals", signer=_OPERATOR
        ),
        cipher=SecretBoxCipher(),
        audit=RecordingSink(),
        strict=StrictSink(),
    )


@pytest.fixture
def store(rig: Rig) -> PrincipalStore:
    return rig.open()


def _principal(
    *,
    name: str = "billing-bot",
    principal_id: str | None = None,
    key: SigningKey | None = None,
    status: str = "pending",
) -> Principal:
    public_key = bytes((key or SigningKey.generate()).verify_key)
    return Principal(
        id=principal_id or uuid.uuid4().hex,
        name=name,
        did=derive_app_did(public_key, org="test"),
        public_key=public_key.hex(),
        secret_digest=uuid.uuid4().hex + uuid.uuid4().hex,
        scopes={Scope.RUNS_READ: (_AGENT,), Scope.GATES_RESOLVE: (_AGENT,)},
        subjects=(_PERSON,),
        status=status,  # type: ignore[arg-type]  # reason: tests feed non-pending statuses on purpose
        created_by=_ACTOR,
        approved_by=None,
        created_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
        last_used_at=None,
        revoked_by=None,
        revoked_at=None,
    )


def _delegation(
    principal: Principal,
    *,
    person: str = _PERSON,
    scopes: tuple[Scope, ...] = (Scope.GATES_RESOLVE,),
    expires_at: datetime = _NOW + timedelta(hours=8),
) -> Delegation:
    return Delegation(
        id=uuid.uuid4().hex,
        person_did=person,
        principal_did=principal.did,
        scopes=scopes,
        created_at=_NOW,
        expires_at=expires_at,
        revoked=False,
    )


def _active(store: PrincipalStore, principal: Principal | None = None) -> Principal:
    pending = store.request(principal or _principal())
    return store.activate(pending.id, _APPROVER, pending.content_hash())


# --- error family ----------------------------------------------------------------


def test_principal_store_error_is_part_of_the_authority_error_family():
    assert issubclass(PrincipalStoreError, UserStoreError)
    assert issubclass(PrincipalStoreError, RuntimeError)


# --- request ---------------------------------------------------------------------


def test_principal_store_new_store_is_empty(store):
    assert store.list() == []
    assert store.get(uuid.uuid4().hex) is None


def test_principal_store_request_stores_a_pending_principal(store):
    principal = _principal()

    returned = store.request(principal)

    assert returned.status == "pending"
    assert returned.approved_by is None
    stored = store.get(principal.id)
    assert stored == returned
    assert stored.content_hash() == principal.content_hash()
    assert store.get_by_did(principal.did) == returned
    assert [p.id for p in store.list()] == [principal.id]


def test_principal_store_request_cannot_self_activate(store):
    """A request carrying status 'active' must never skip approval."""
    sneaky = _principal(status="active")
    try:
        store.request(sneaky)
    except PrincipalStoreError:
        pass
    stored = store.get(sneaky.id)
    assert stored is None or stored.status == "pending"


def test_principal_store_request_refuses_duplicate_id(store):
    first = _principal()
    store.request(first)
    clash = _principal(principal_id=first.id, name="other")

    with pytest.raises(PrincipalStoreError):
        store.request(clash)

    assert store.get(first.id) == store.list()[0]
    assert store.get(first.id).name == first.name


def test_principal_store_request_refuses_duplicate_did(store):
    key = SigningKey.generate()
    first = _principal(key=key)
    store.request(first)

    with pytest.raises(PrincipalStoreError):
        store.request(_principal(key=key, name="clone"))

    assert [p.id for p in store.list()] == [first.id]


def test_principal_store_get_by_did_unknown_is_none(store):
    store.request(_principal())
    assert store.get_by_did(_principal().did) is None


# --- activate --------------------------------------------------------------------


def test_principal_store_activate_with_matching_hash_activates(store):
    pending = store.request(_principal())

    active = store.activate(pending.id, _APPROVER, pending.content_hash())

    assert active.status == "active"
    assert active.approved_by == _APPROVER
    assert active.content_hash() == pending.content_hash()
    assert store.get(pending.id) == active


def test_principal_store_activate_with_wrong_hash_is_refused(store):
    pending = store.request(_principal())

    with pytest.raises(PrincipalStoreError):
        store.activate(pending.id, _APPROVER, "0" * 64)

    still = store.get(pending.id)
    assert still.status == "pending"
    assert still.approved_by is None


def test_principal_store_activate_hash_of_a_different_principal_is_refused(store):
    pending = store.request(_principal())
    other = _principal()

    with pytest.raises(PrincipalStoreError):
        store.activate(pending.id, _APPROVER, other.content_hash())

    assert store.get(pending.id).status == "pending"


def test_principal_store_activate_already_active_is_refused(store):
    active = _active(store)

    with pytest.raises(PrincipalStoreError):
        store.activate(active.id, _APPROVER, active.content_hash())


def test_principal_store_activate_revoked_is_refused(store):
    pending = store.request(_principal())
    store.revoke(pending.id, _ACTOR)

    with pytest.raises(PrincipalStoreError):
        store.activate(pending.id, _APPROVER, pending.content_hash())

    assert store.get(pending.id).status == "revoked"


def test_principal_store_activate_unknown_id_is_refused(store):
    with pytest.raises(PrincipalStoreError):
        store.activate(uuid.uuid4().hex, _APPROVER, "0" * 64)


# --- revoke ----------------------------------------------------------------------


def test_principal_store_revoke_sets_revoked_by_and_at(store):
    active = _active(store)

    revoked = store.revoke(active.id, _APPROVER)

    assert revoked.status == "revoked"
    assert revoked.revoked_by == _APPROVER
    assert revoked.revoked_at is not None
    assert revoked.revoked_at.tzinfo is not None
    assert store.get(active.id) == revoked


def test_principal_store_revoke_persists_across_reopen(rig, store):
    active = _active(store)
    store.revoke(active.id, _ACTOR)

    reopened = rig.open()

    reread = reopened.get(active.id)
    assert reread.status == "revoked"
    assert reread.revoked_by == _ACTOR
    assert reopened.get_by_did(active.did).status == "revoked"


def test_principal_store_revoke_unknown_id_is_refused(store):
    with pytest.raises(PrincipalStoreError):
        store.revoke(uuid.uuid4().hex, _ACTOR)


# --- rollback --------------------------------------------------------------------


def test_principal_store_restored_older_file_is_detected_on_every_read(rig, store):
    """Restoring a file from before the revocation must not un-revoke the app."""
    active = _active(store)  # two revisions: request, activate
    before_revoke = rig.path.read_bytes()
    store.revoke(active.id, _ACTOR)
    store.request(_principal(name="later"))  # head is now two revisions past the copy
    rig.path.write_bytes(before_revoke)

    for reader in (store, rig.open()):
        with pytest.raises(AuthorityStateError):
            reader.get(active.id)
        with pytest.raises(AuthorityStateError):
            reader.get_by_did(active.did)
        with pytest.raises(AuthorityStateError):
            reader.list()
        with pytest.raises(AuthorityStateError):
            reader.delegations_for(_PERSON)
        with pytest.raises(AuthorityStateError):
            reader.live_delegation(_PERSON, active.did, Scope.GATES_RESOLVE, _NOW)


def test_principal_store_file_written_owner_only_and_sealed(rig, store):
    principal = _principal()
    store.request(principal)

    assert rig.path.stat().st_mode & 0o777 == 0o600
    raw = rig.path.read_bytes()
    assert principal.secret_digest.encode() not in raw
    assert principal.name.encode() not in raw


# --- touch -----------------------------------------------------------------------


def test_principal_store_touch_records_last_used(store):
    active = _active(store)

    store.touch(active.id, _NOW)

    assert store.get(active.id).last_used_at == _NOW


def test_principal_store_touch_within_a_minute_does_not_write(rig, store):
    active = _active(store)
    store.touch(active.id, _NOW)
    head = rig.anchor.latest()

    store.touch(active.id, _NOW + timedelta(seconds=59))

    assert rig.anchor.latest() == head
    assert store.get(active.id).last_used_at == _NOW


def test_principal_store_touch_after_a_minute_writes_again(rig, store):
    active = _active(store)
    store.touch(active.id, _NOW)
    head = rig.anchor.latest()
    later = _NOW + timedelta(seconds=61)

    store.touch(active.id, later)

    assert rig.anchor.latest().version == head.version + 1
    assert store.get(active.id).last_used_at == later


def test_principal_store_touch_does_not_change_the_approved_hash(store):
    active = _active(store)
    store.touch(active.id, _NOW)
    assert store.get(active.id).content_hash() == active.content_hash()


# --- delegations -----------------------------------------------------------------


def test_principal_store_delegation_add_and_list_for_person(store):
    app = _active(store)
    mine = _delegation(app)
    theirs = _delegation(app, person=_OTHER_PERSON)

    store.add_delegation(mine)
    store.add_delegation(theirs)

    assert store.delegations_for(_PERSON) == [mine]
    assert store.delegations_for(_OTHER_PERSON) == [theirs]


def test_principal_store_delegations_survive_reopen(rig, store):
    app = _active(store)
    delegation = _delegation(app)
    store.add_delegation(delegation)

    assert rig.open().delegations_for(_PERSON) == [delegation]


def test_principal_store_live_delegation_found_inside_window(store):
    app = _active(store)
    delegation = _delegation(app)
    store.add_delegation(delegation)

    found = store.live_delegation(
        _PERSON, app.did, Scope.GATES_RESOLVE, _NOW + timedelta(hours=7, minutes=59)
    )

    assert found == delegation


def test_principal_store_live_delegation_expired_is_none(store):
    app = _active(store)
    delegation = _delegation(app)
    store.add_delegation(delegation)

    assert (
        store.live_delegation(_PERSON, app.did, Scope.GATES_RESOLVE, delegation.expires_at) is None
    )
    assert (
        store.live_delegation(
            _PERSON, app.did, Scope.GATES_RESOLVE, delegation.expires_at + timedelta(seconds=1)
        )
        is None
    )


def test_principal_store_live_delegation_wrong_scope_person_or_app_is_none(store):
    app = _active(store)
    other_app = _active(store, _principal(name="other-app"))
    store.add_delegation(_delegation(app, scopes=(Scope.GATES_RESOLVE,)))

    assert store.live_delegation(_PERSON, app.did, Scope.TASKS_WRITE, _NOW) is None
    assert store.live_delegation(_OTHER_PERSON, app.did, Scope.GATES_RESOLVE, _NOW) is None
    assert store.live_delegation(_PERSON, other_app.did, Scope.GATES_RESOLVE, _NOW) is None


def test_principal_store_revoked_delegation_is_not_live(store):
    app = _active(store)
    delegation = _delegation(app)
    store.add_delegation(delegation)

    store.revoke_delegation(delegation.id, _PERSON)

    assert store.live_delegation(_PERSON, app.did, Scope.GATES_RESOLVE, _NOW) is None
    [listed] = store.delegations_for(_PERSON)
    assert listed.revoked is True


def test_principal_store_revoke_delegation_by_non_owner_is_refused(store):
    app = _active(store)
    delegation = _delegation(app)
    store.add_delegation(delegation)

    with pytest.raises(PrincipalStoreError):
        store.revoke_delegation(delegation.id, _OTHER_PERSON)

    assert store.live_delegation(_PERSON, app.did, Scope.GATES_RESOLVE, _NOW) == delegation


def test_principal_store_revoke_unknown_delegation_is_refused(store):
    with pytest.raises(PrincipalStoreError):
        store.revoke_delegation(uuid.uuid4().hex, _PERSON)


# --- durable audit gate ----------------------------------------------------------


def _snapshot(store: PrincipalStore) -> tuple[list[dict[str, object]], list[Delegation]]:
    return [p.to_dict() for p in store.list()], store.delegations_for(_PERSON)


@pytest.mark.parametrize("fail_prefix", ["", "principal."])
def test_principal_store_durable_audit_failure_refuses_every_mutation(rig, store, fail_prefix):
    """A mutation whose durable audit append fails must leave the authority unchanged.

    ``""`` fails every durable append; ``"principal."`` fails only the principal.* event,
    which proves that event is itself durable and gates the commit.
    """
    pending = store.request(_principal())
    app = _active(store)
    delegation = _delegation(app)
    store.add_delegation(delegation)
    before = _snapshot(store)
    head = rig.anchor.latest()
    rig.strict.fail_prefix = fail_prefix

    attempts = [
        lambda: store.request(_principal(name="blocked")),
        lambda: store.activate(pending.id, _APPROVER, pending.content_hash()),
        lambda: store.revoke(app.id, _ACTOR),
        lambda: store.add_delegation(_delegation(app, person=_PERSON)),
        lambda: store.revoke_delegation(delegation.id, _PERSON),
    ]
    for attempt in attempts:
        with pytest.raises((OSError, UserStoreError)):
            attempt()

    rig.strict.fail_prefix = None
    assert rig.anchor.latest() == head
    assert _snapshot(rig.open()) == before


# --- audit events ----------------------------------------------------------------


def test_principal_store_each_mutation_emits_its_event_with_the_bound_actor(rig, store):
    principal = _principal()
    pending = store.request(principal)
    store.activate(pending.id, _ACTOR, pending.content_hash())
    delegation = _delegation(pending)
    store.add_delegation(delegation)
    store.revoke_delegation(delegation.id, _PERSON)
    store.revoke(pending.id, _ACTOR)

    events = rig.mutation_events()

    assert [e.action for e in events] == [
        "principal.request",
        "principal.activate",
        "principal.delegation.add",
        "principal.delegation.revoke",
        "principal.revoke",
    ]
    assert {e.actor_did for e in events} == {_ACTOR}
    assert all(e in rig.strict.events for e in events), "principal.* must be durable"


def test_principal_store_audit_actor_is_the_bound_actor_not_a_caller_argument(rig):
    store = rig.open(actor_did=_ACTOR)
    pending = store.request(_principal())
    store.activate(pending.id, _APPROVER, pending.content_hash())

    [activate] = [e for e in rig.mutation_events() if e.action == "principal.activate"]
    assert activate.actor_did == _ACTOR


def test_principal_store_audit_never_carries_the_secret_digest(rig, store):
    principal = _principal()
    pending = store.request(principal)
    store.activate(pending.id, _APPROVER, pending.content_hash())
    store.revoke(pending.id, _ACTOR)

    dumped = json.dumps([e.model_dump(mode="json") for e in rig.all_events()])
    assert rig.mutation_events()
    assert principal.secret_digest not in dumped


def test_principal_store_refused_activation_emits_no_allow(rig, store):
    pending = store.request(_principal())
    with pytest.raises(PrincipalStoreError):
        store.activate(pending.id, _APPROVER, "0" * 64)

    activates = [e for e in rig.mutation_events() if e.action == "principal.activate"]
    assert all(e.outcome != "allow" for e in activates)


# --- package root ----------------------------------------------------------------


def test_principal_store_and_scope_are_exported_from_the_package_root():
    import arctrust

    assert arctrust.PrincipalStore is PrincipalStore
    assert arctrust.Scope is Scope
    assert "PrincipalStore" in arctrust.__all__
    assert "Scope" in arctrust.__all__


def test_account_authority_offers_principal_store_beside_user_store():
    from arctrust.authority import AccountAuthority

    assert callable(getattr(AccountAuthority, "principal_store", None))
