"""CallbackAllowlistStore: the sealed, anchored ``callbacks`` authority record (SPEC-088 COMP-018).

Contract pinned here for the builder (T-1368), module ``arctrust.callback_allowlist``:

- ``CALLBACK_ALLOWLIST_TOOL = "work_event_callback"`` — ``PendingApproval.tool`` of an
  enterprise/federal add request.
- ``normalize_origin(value: str) -> str`` — ``scheme://host[:port]`` only. Lowercases scheme
  and host, drops the default port (443 for https, 80 for http), keeps any other port and
  IPv6 brackets. Refuses (``CallbackOriginError``, ``reason == "invalid_origin"``) any path
  (including ``/``), query, fragment, userinfo, a scheme other than http/https, a missing
  host, or a bad port.
- ``NewCallbackEntry(origin: str, allow_private: bool = False,
  clearance: Classification = Classification.UNCLASSIFIED)`` — the add request.
- ``AllowlistEntry`` (frozen dataclass): ``id`` (uuid4 hex), ``origin`` (normalized),
  ``allow_private``, ``clearance`` (``arctrust.classification.Classification``),
  ``status`` (``"pending" | "active" | "removed"``), ``created_by``, ``approved_by``,
  ``created_at``; ``content_hash()`` is SHA-256 hex over the authority fields (id, origin,
  allow_private, clearance, created_by, created_at) — lifecycle fields (status,
  approved_by) excluded so approving never changes the hash; ``to_dict()``/``from_dict()``.
- ``CallbackAllowlistStore(path, *, anchor, cipher, audit_sink, strict_audit_sink,
  actor_did, tier)`` on ``SealedAuthorityState(record_name="callbacks")``:
  ``list()``; ``request(entry, actor_did)`` (personal: ``active`` with
  ``approved_by == actor_did``; enterprise/federal: ``pending``);
  ``activate(entry_id, approver_did, expected_hash)``; ``remove(entry_id, actor_did)``.
  Every query and mutation reloads the anchored head, so rollback surfaces on every use.
  The sealed payload is ``{"entries": [AllowlistEntry.to_dict(), ...]}`` (clearance stored
  as its name, e.g. ``"CUI"``; ``created_at`` ISO-8601).
- Refusals raise ``CallbackAllowlistError`` (a ``UserStoreError``) with a stable ``reason``:
  ``invalid_origin``, ``duplicate_origin`` (an active or pending entry already holds the
  origin), ``unknown_entry``, ``not_pending``, ``content_hash_mismatch``,
  ``second_operator_required`` (federal approver == requester), ``already_removed``.
- Every mutation first appends a durable ``work_event.callback_allowlist.<verb>`` event
  (verbs ``request``, ``activate``, ``remove``) attributed to the bound actor; a failed
  append refuses with no state change. Refusals write an ``outcome="deny"`` event.
- ``AccountAuthority.callback_allowlist_store(actor_proof, *, tier)`` binds it beside the
  principals pair (own ``VaultKVAnchor(record="callbacks")`` and
  ``VaultCipher(record_id="callbacks")``).

Requirements: REQ-625 (allowlist is a sealed anchored record, second step above personal,
every change audited), REQ-631 (audit), REQ-632 (rolled-back allowlist fails closed).

Real collaborators: ``FileJournalAnchor`` signed by ``InProcessSigner`` on the real
filesystem. Only the cipher (Vault custody) and audit sinks are doubles; the cipher is an
authenticated NaCl SecretBox so tampering is detected, not simulated.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from nacl.secret import SecretBox

from arctrust import FileJournalAnchor, InProcessSigner
from arctrust.audit import AuditEvent
from arctrust.classification import Classification
from arctrust.sealed_state import AuthorityStateError
from arctrust.users import UserStoreError

_ACTOR = "did:arc:test:operator/callback-store-actor"
_REQUESTER = "did:arc:test:user/requester-ada"
_APPROVER = "did:arc:test:user/approver-bob"
_OPERATOR = InProcessSigner(bytes(range(32)))
_ORIGIN = "https://hooks.example.com"
_MUTATIONS = {
    "work_event.callback_allowlist.request",
    "work_event.callback_allowlist.activate",
    "work_event.callback_allowlist.remove",
}


# --- external-boundary doubles ---------------------------------------------------


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
    """Durable audit chain double; refuses appends whose action starts with ``fail_prefix``."""

    events: list[AuditEvent] = field(default_factory=list)
    fail_prefix: str | None = None

    def write_durable(self, event: AuditEvent) -> None:
        if self.fail_prefix is not None and event.action.startswith(self.fail_prefix):
            raise OSError("injected durable append failure")
        self.events.append(event)


# --- fixtures --------------------------------------------------------------------


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@dataclass
class Rig:
    path: Path
    anchor: FileJournalAnchor
    cipher: SecretBoxCipher = field(default_factory=SecretBoxCipher)
    audit: RecordingSink = field(default_factory=RecordingSink)
    strict: StrictSink = field(default_factory=StrictSink)

    def open(self, tier: str = "personal", actor_did: str = _ACTOR):  # type: ignore[no-untyped-def]  # reason: store type is the unit under test
        from arctrust.callback_allowlist import CallbackAllowlistStore

        return CallbackAllowlistStore(
            self.path,
            anchor=self.anchor,
            cipher=self.cipher,
            audit_sink=self.audit,
            strict_audit_sink=self.strict,
            actor_did=actor_did,
            tier=tier,
        )

    def all_events(self) -> list[AuditEvent]:
        return [*self.audit.events, *self.strict.events]

    def mutation_events(self) -> list[AuditEvent]:
        return [e for e in self.all_events() if e.action in _MUTATIONS]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(
        path=_private_dir(tmp_path / "state") / "callbacks.json",
        anchor=FileJournalAnchor(
            _private_dir(tmp_path / "anchor"), scope="test/callbacks", signer=_OPERATOR
        ),
    )


def _new(origin: str = _ORIGIN, **kw):  # type: ignore[no-untyped-def]  # reason: model is the unit under test
    from arctrust.callback_allowlist import NewCallbackEntry

    return NewCallbackEntry(origin=origin, **kw)


def _reason(excinfo: pytest.ExceptionInfo[BaseException]) -> str:
    return str(getattr(excinfo.value, "reason", ""))


# --- fixture smoke: proves the rig, not the feature -------------------------------


def test_fixture_smoke_anchor_and_cipher_round_trip(rig: Rig) -> None:
    sealed = rig.cipher.seal(b"x")
    assert rig.cipher.open(sealed) == b"x"
    assert rig.anchor.latest() is None


# --- error family and constants ---------------------------------------------------


def test_callback_allowlist_error_is_part_of_the_authority_error_family() -> None:
    from arctrust.callback_allowlist import CallbackAllowlistError

    assert issubclass(CallbackAllowlistError, UserStoreError)


def test_callback_allowlist_tool_name_is_work_event_callback() -> None:
    from arctrust.callback_allowlist import CALLBACK_ALLOWLIST_TOOL

    assert CALLBACK_ALLOWLIST_TOOL == "work_event_callback"


# --- origin normalization ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://hooks.example.com", "https://hooks.example.com"),
        ("HTTPS://Hooks.Example.COM", "https://hooks.example.com"),
        ("https://hooks.example.com:443", "https://hooks.example.com"),
        ("http://hooks.example.com:80", "http://hooks.example.com"),
        ("https://hooks.example.com:8443", "https://hooks.example.com:8443"),
        ("https://10.0.0.5:9000", "https://10.0.0.5:9000"),
        ("https://[fe80::1]", "https://[fe80::1]"),
    ],
)
def test_normalize_origin_accepts_scheme_host_port(raw: str, expected: str) -> None:
    from arctrust.callback_allowlist import normalize_origin

    assert normalize_origin(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://hooks.example.com/",
        "https://hooks.example.com/callbacks",
        "https://hooks.example.com?x=1",
        "https://hooks.example.com#frag",
        "https://user@hooks.example.com",
        "https://user:pw@hooks.example.com",
        "ftp://hooks.example.com",
        "hooks.example.com",
        "https://",
        "https://hooks.example.com:0",
        "https://hooks.example.com:99999",
        "",
    ],
)
def test_normalize_origin_refuses_path_userinfo_query_and_bad_shapes(raw: str) -> None:
    from arctrust.callback_allowlist import CallbackOriginError, normalize_origin

    with pytest.raises(CallbackOriginError) as excinfo:
        normalize_origin(raw)
    assert _reason(excinfo) == "invalid_origin"


# --- request by tier ---------------------------------------------------------------


def test_store_new_store_is_empty(rig: Rig) -> None:
    assert rig.open().list() == []


def test_store_personal_add_is_active_at_once(rig: Rig) -> None:
    store = rig.open("personal")

    entry = store.request(_new(), _REQUESTER)

    assert entry.status == "active"
    assert entry.origin == _ORIGIN
    assert entry.created_by == _REQUESTER
    assert entry.approved_by == _REQUESTER
    assert entry.allow_private is False
    assert entry.clearance == Classification.UNCLASSIFIED
    assert [e.id for e in rig.open("personal").list()] == [entry.id]
    assert rig.open("personal").list()[0].status == "active"


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
def test_store_add_above_personal_is_pending(rig: Rig, tier: str) -> None:
    store = rig.open(tier)

    entry = store.request(_new(allow_private=True, clearance=Classification.CUI), _REQUESTER)

    assert entry.status == "pending"
    assert entry.approved_by is None
    (stored,) = rig.open(tier).list()
    assert stored.status == "pending"
    assert stored.allow_private is True
    assert stored.clearance == Classification.CUI


def test_store_request_normalizes_the_origin(rig: Rig) -> None:
    entry = rig.open().request(_new("HTTPS://Hooks.Example.com:443"), _REQUESTER)
    assert entry.origin == _ORIGIN


@pytest.mark.parametrize(
    "origin",
    [
        "https://hooks.example.com/callbacks",
        "https://user:pw@hooks.example.com",
        "https://hooks.example.com?q=1",
    ],
)
def test_store_request_refuses_origin_with_path_userinfo_or_query_and_writes_nothing(
    rig: Rig, origin: str
) -> None:
    from arctrust.callback_allowlist import CallbackAllowlistError

    store = rig.open()
    with pytest.raises(CallbackAllowlistError) as excinfo:
        store.request(_new(origin), _REQUESTER)

    assert _reason(excinfo) == "invalid_origin"
    assert rig.anchor.latest() is None
    assert not [e for e in rig.mutation_events() if e.outcome == "allow"]


def test_store_request_refuses_a_duplicate_live_origin(rig: Rig) -> None:
    from arctrust.callback_allowlist import CallbackAllowlistError

    store = rig.open()
    store.request(_new(), _REQUESTER)
    with pytest.raises(CallbackAllowlistError) as excinfo:
        store.request(_new("https://HOOKS.example.com:443"), _REQUESTER)
    assert _reason(excinfo) == "duplicate_origin"
    assert len(store.list()) == 1


def test_store_origin_may_be_added_again_after_removal(rig: Rig) -> None:
    store = rig.open()
    first = store.request(_new(), _REQUESTER)
    store.remove(first.id, _REQUESTER)

    second = store.request(_new(), _REQUESTER)

    assert second.id != first.id
    assert {e.id: e.status for e in store.list()} == {first.id: "removed", second.id: "active"}


# --- activate (enterprise / federal second step) -----------------------------------


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
def test_store_activate_with_matching_hash_activates(rig: Rig, tier: str) -> None:
    store = rig.open(tier)
    pending = store.request(_new(), _REQUESTER)

    active = store.activate(pending.id, _APPROVER, pending.content_hash())

    assert active.status == "active"
    assert active.approved_by == _APPROVER
    assert active.content_hash() == pending.content_hash()
    assert rig.open(tier).list()[0].status == "active"


def test_store_activate_with_wrong_hash_is_refused_and_stays_pending(rig: Rig) -> None:
    from arctrust.callback_allowlist import CallbackAllowlistError

    store = rig.open("enterprise")
    pending = store.request(_new(), _REQUESTER)

    with pytest.raises(CallbackAllowlistError) as excinfo:
        store.activate(pending.id, _APPROVER, "0" * 64)

    assert _reason(excinfo) == "content_hash_mismatch"
    assert store.list()[0].status == "pending"


def test_store_activate_with_the_hash_of_another_entry_is_refused(rig: Rig) -> None:
    from arctrust.callback_allowlist import CallbackAllowlistError

    store = rig.open("enterprise")
    target = store.request(_new(), _REQUESTER)
    other = store.request(_new("https://other.example.com"), _REQUESTER)

    with pytest.raises(CallbackAllowlistError) as excinfo:
        store.activate(target.id, _APPROVER, other.content_hash())
    assert _reason(excinfo) == "content_hash_mismatch"


def test_store_content_hash_binds_origin_allow_private_and_clearance() -> None:
    from datetime import UTC, datetime

    from arctrust.callback_allowlist import AllowlistEntry

    base = AllowlistEntry(
        id="e" * 32,
        origin=_ORIGIN,
        allow_private=False,
        clearance=Classification.UNCLASSIFIED,
        status="pending",
        created_by=_REQUESTER,
        approved_by=None,
        created_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    from dataclasses import replace

    variants = [
        replace(base, origin="https://evil.example.com"),
        replace(base, allow_private=True),
        replace(base, clearance=Classification.SECRET),
    ]
    assert all(v.content_hash() != base.content_hash() for v in variants)
    assert replace(base, status="active", approved_by=_APPROVER).content_hash() == (
        base.content_hash()
    )


def test_store_federal_approver_equal_to_requester_is_refused(rig: Rig) -> None:
    from arctrust.callback_allowlist import CallbackAllowlistError

    store = rig.open("federal")
    pending = store.request(_new(), _REQUESTER)

    with pytest.raises(CallbackAllowlistError) as excinfo:
        store.activate(pending.id, _REQUESTER, pending.content_hash())

    assert _reason(excinfo) == "second_operator_required"
    assert store.list()[0].status == "pending"


def test_store_enterprise_requester_may_approve_their_own_request(rig: Rig) -> None:
    store = rig.open("enterprise")
    pending = store.request(_new(), _REQUESTER)

    active = store.activate(pending.id, _REQUESTER, pending.content_hash())

    assert active.status == "active"


def test_store_activate_already_active_is_refused(rig: Rig) -> None:
    from arctrust.callback_allowlist import CallbackAllowlistError

    store = rig.open("personal")
    entry = store.request(_new(), _REQUESTER)
    with pytest.raises(CallbackAllowlistError) as excinfo:
        store.activate(entry.id, _APPROVER, entry.content_hash())
    assert _reason(excinfo) == "not_pending"


def test_store_activate_removed_entry_is_refused(rig: Rig) -> None:
    """Deny-then-approve race: a removed pending entry never comes back."""
    from arctrust.callback_allowlist import CallbackAllowlistError

    store = rig.open("enterprise")
    pending = store.request(_new(), _REQUESTER)
    store.remove(pending.id, _APPROVER)
    with pytest.raises(CallbackAllowlistError) as excinfo:
        store.activate(pending.id, _APPROVER, pending.content_hash())
    assert _reason(excinfo) == "not_pending"
    assert store.list()[0].status == "removed"


def test_store_activate_unknown_id_is_refused(rig: Rig) -> None:
    from arctrust.callback_allowlist import CallbackAllowlistError

    with pytest.raises(CallbackAllowlistError) as excinfo:
        rig.open("enterprise").activate("f" * 32, _APPROVER, "0" * 64)
    assert _reason(excinfo) == "unknown_entry"


# --- remove ------------------------------------------------------------------------


def test_store_remove_is_visible_to_a_fresh_reader_at_once(rig: Rig) -> None:
    store = rig.open()
    entry = store.request(_new(), _REQUESTER)
    reader = rig.open()
    assert reader.list()[0].status == "active"  # reader has loaded once

    store.remove(entry.id, _REQUESTER)

    # The same reader instance sees the removal on its next call: no cache.
    assert reader.list()[0].status == "removed"


def test_store_remove_unknown_and_twice_are_refused(rig: Rig) -> None:
    from arctrust.callback_allowlist import CallbackAllowlistError

    store = rig.open()
    entry = store.request(_new(), _REQUESTER)
    store.remove(entry.id, _REQUESTER)

    with pytest.raises(CallbackAllowlistError) as twice:
        store.remove(entry.id, _REQUESTER)
    with pytest.raises(CallbackAllowlistError) as unknown:
        store.remove("f" * 32, _REQUESTER)

    assert _reason(twice) == "already_removed"
    assert _reason(unknown) == "unknown_entry"


def test_store_stale_instance_does_not_lose_another_writers_entry(rig: Rig) -> None:
    """Two writers interleave: each mutation reloads the head, so neither entry is lost."""
    first, second = rig.open(), rig.open()
    first.list()
    second.request(_new("https://a.example.com"), _REQUESTER)

    first.request(_new("https://b.example.com"), _REQUESTER)

    origins = sorted(e.origin for e in rig.open().list())
    assert origins == ["https://a.example.com", "https://b.example.com"]


# --- rollback ----------------------------------------------------------------------


def test_store_restored_older_record_is_detected_and_every_read_refuses(rig: Rig) -> None:
    """Restoring a file from before a removal must not bring the origin back."""
    store = rig.open()
    entry = store.request(_new(), _REQUESTER)
    before_remove = rig.path.read_bytes()
    store.remove(entry.id, _REQUESTER)
    store.request(_new("https://later.example.com"), _REQUESTER)  # two revisions past the copy
    rig.path.write_bytes(before_remove)

    for reader in (store, rig.open()):
        with pytest.raises(AuthorityStateError):
            reader.list()
        with pytest.raises(AuthorityStateError):
            reader.request(_new("https://third.example.com"), _REQUESTER)
        with pytest.raises(AuthorityStateError):
            reader.remove(entry.id, _REQUESTER)


def test_store_file_is_owner_only_and_sealed(rig: Rig) -> None:
    rig.open().request(_new(), _REQUESTER)

    assert rig.path.stat().st_mode & 0o777 == 0o600
    assert b"hooks.example.com" not in rig.path.read_bytes()


# --- durable audit gate ------------------------------------------------------------


@pytest.mark.parametrize("fail_prefix", ["", "work_event.callback_allowlist."])
def test_store_strict_audit_failure_refuses_every_mutation(rig: Rig, fail_prefix: str) -> None:
    store = rig.open("enterprise")
    pending = store.request(_new(), _REQUESTER)
    active = store.request(_new("https://active.example.com"), _REQUESTER)
    store.activate(active.id, _APPROVER, active.content_hash())
    before = [e.to_dict() for e in store.list()]
    head = rig.anchor.latest()
    rig.strict.fail_prefix = fail_prefix

    attempts = [
        lambda: store.request(_new("https://blocked.example.com"), _REQUESTER),
        lambda: store.activate(pending.id, _APPROVER, pending.content_hash()),
        lambda: store.remove(active.id, _REQUESTER),
    ]
    for attempt in attempts:
        with pytest.raises((OSError, UserStoreError)):
            attempt()

    rig.strict.fail_prefix = None
    assert rig.anchor.latest() == head
    assert [e.to_dict() for e in rig.open("enterprise").list()] == before


def test_store_each_mutation_emits_its_durable_event_with_the_bound_actor(rig: Rig) -> None:
    store = rig.open("enterprise", actor_did=_ACTOR)
    pending = store.request(_new(), _REQUESTER)
    store.activate(pending.id, _APPROVER, pending.content_hash())
    store.remove(pending.id, _REQUESTER)

    events = rig.mutation_events()

    assert [e.action for e in events if e.outcome == "allow"] == [
        "work_event.callback_allowlist.request",
        "work_event.callback_allowlist.activate",
        "work_event.callback_allowlist.remove",
    ]
    assert {e.actor_did for e in events} == {_ACTOR}
    assert all(e in rig.strict.events for e in events if e.outcome == "allow")
    assert all(e.target == pending.id for e in events)


def test_store_refusal_writes_a_deny_event_and_no_allow(rig: Rig) -> None:
    from arctrust.callback_allowlist import CallbackAllowlistError

    store = rig.open("federal")
    pending = store.request(_new(), _REQUESTER)
    with pytest.raises(CallbackAllowlistError):
        store.activate(pending.id, _REQUESTER, pending.content_hash())

    activates = [
        e for e in rig.mutation_events() if e.action == "work_event.callback_allowlist.activate"
    ]
    assert activates
    assert all(e.outcome == "deny" for e in activates)


# --- account authority -------------------------------------------------------------


def test_account_authority_offers_callback_allowlist_store_beside_principal_store() -> None:
    from arctrust.authority import AccountAuthority

    assert callable(getattr(AccountAuthority, "callback_allowlist_store", None))
