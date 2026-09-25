"""The user store is what makes an approval attributable to a person."""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass, field

import pytest
from nacl.signing import SigningKey

from arctrust.audit import AuditEvent
from arctrust.monotonic import AnchorHead
from arctrust.users import OPERATOR, VIEWER, UserStore, UserStoreError

GOOD = "correct-horse-battery"


class FakeIssuer:
    def __init__(self):
        self.keys: dict[str, SigningKey] = {}

    def create_user_key(self, key_ref: str) -> bytes:
        self.keys[key_ref] = SigningKey.generate()
        return self.public_key(key_ref)

    def public_key(self, key_ref: str) -> bytes:
        return bytes(self.keys[key_ref].verify_key)


class FakeAnchor:
    scope = "test/users"

    def __init__(self):
        self.head: AnchorHead | None = None

    def latest(self) -> AnchorHead | None:
        return self.head

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        if expected != self.head:
            raise RuntimeError("stale user authority writer")
        self.head = AnchorHead(
            scope=self.scope,
            version=1 if expected is None else expected.version + 1,
            digest=digest,
            previous_digest=None if expected is None else expected.digest,
            intent=intent,
        )
        return self.head


class FakeCipher:
    def seal(self, payload: bytes) -> str:
        return base64.b64encode(payload).decode()

    def open(self, sealed: str) -> bytes:
        return base64.b64decode(sealed, validate=True)


@dataclass
class FakeAudit:
    events: list[AuditEvent] = field(default_factory=list)
    fail: bool = False

    def write(self, event: AuditEvent) -> None:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append(event)


@pytest.fixture
def authority():
    return dict(
        issuer=FakeIssuer(),
        anchor=FakeAnchor(),
        cipher=FakeCipher(),
        audit_sink=FakeAudit(),
        actor_did="did:arc:test:user/audit",
    )


@pytest.fixture
def store(tmp_path, authority):
    return UserStore(tmp_path / "users.json", **authority)


def test_a_new_deployment_has_nobody(store):
    assert store.is_empty()
    assert store.list() == []


def test_a_user_gets_an_identity_of_their_own(store):
    user = store.add("Josh@Example.com ", GOOD, roles=(OPERATOR,))
    assert user.email == "josh@example.com"  # normalized, so login is case-insensitive
    assert user.did.startswith("did:arc:arc:user/")
    assert user.is_operator
    assert len(bytes.fromhex(user.public_key)) == 32


def test_two_users_are_two_identities(store):
    a = store.add("a@example.com", GOOD)
    b = store.add("b@example.com", GOOD)
    assert a.did != b.did
    assert a.signing_key_ref != b.signing_key_ref


def test_the_password_is_never_stored(store):
    store.add("a@example.com", GOOD)
    raw = (store.path).read_text()
    assert GOOD not in raw
    assert "$argon2id$" not in raw


def test_login_accepts_the_right_password_and_nothing_else(store):
    store.add("a@example.com", GOOD)
    assert store.verify("a@example.com", GOOD) is not None
    assert store.verify("A@Example.com", GOOD) is not None
    assert store.verify("a@example.com", "wrong-password-here") is None
    assert store.verify("nobody@example.com", GOOD) is None


def test_a_disabled_user_cannot_log_in(store):
    store.add("a@example.com", GOOD)
    store.set_disabled("a@example.com", True)
    assert store.verify("a@example.com", GOOD) is None


def test_short_passwords_are_refused(store):
    with pytest.raises(ValueError, match="12 characters"):
        store.add("a@example.com", "short")


def test_duplicate_email_is_refused(store):
    store.add("a@example.com", GOOD)
    with pytest.raises(ValueError, match="already exists"):
        store.add("A@EXAMPLE.COM", GOOD)


def test_changing_a_password_invalidates_the_old_one(store):
    store.add("a@example.com", GOOD)
    store.set_password("a@example.com", "a-different-passphrase")
    assert store.verify("a@example.com", GOOD) is None
    assert store.verify("a@example.com", "a-different-passphrase") is not None


def test_the_last_operator_cannot_be_removed(store):
    """Otherwise the deployment has nobody who can approve anything again."""
    store.add("boss@example.com", GOOD, roles=(OPERATOR,))
    store.add("viewer@example.com", GOOD)
    with pytest.raises(ValueError, match="only operator"):
        store.remove("boss@example.com")

    store.set_roles("viewer@example.com", (OPERATOR,))
    store.remove("boss@example.com")
    assert [u.email for u in store.list()] == ["viewer@example.com"]


def test_a_paired_surface_identity_resolves_to_its_user(store):
    """A reset has to find the person from the account they messaged from."""
    store.add("a@example.com", GOOD, pairings={"someplatform": "4242"})
    assert store.by_pairing("someplatform", "4242").email == "a@example.com"
    assert store.by_pairing("someplatform", "9999") is None
    # The platform half matters: the same id elsewhere is a different person.
    assert store.by_pairing("otherplatform", "4242") is None


def test_the_same_surface_id_cannot_belong_to_two_people(store):
    """Otherwise every message from it is ambiguous about who sent it."""
    store.add("a@example.com", GOOD, pairings={"someplatform": "4242"})
    store.add("b@example.com", GOOD)
    with pytest.raises(ValueError, match=r"already paired to a@example\.com"):
        store.set_pairing("b@example.com", "someplatform", "4242")


def test_a_person_can_hold_several_surfaces_at_once(store):
    store.add("a@example.com", GOOD)
    store.set_pairing("a@example.com", "someplatform", "4242")
    store.set_pairing("a@example.com", "otherplatform", "U99")
    assert store.get("a@example.com").pairings == {
        "someplatform": "4242",
        "otherplatform": "U99",
    }
    store.set_pairing("a@example.com", "someplatform", None)
    assert store.get("a@example.com").pairings == {"otherplatform": "U99"}


def test_users_survive_a_reload(store, tmp_path, authority):
    store.add("a@example.com", GOOD, roles=(OPERATOR, VIEWER), pairings={"someplatform": "4242"})
    reopened = UserStore(tmp_path / "users.json", **authority)
    user = reopened.get("a@example.com")
    assert user.pairings == {"someplatform": "4242"}
    assert set(user.roles) == {OPERATOR, VIEWER}
    assert reopened.verify("a@example.com", GOOD) is not None


def test_the_store_is_written_owner_only(store):
    store.add("a@example.com", GOOD)
    assert oct(store.path.stat().st_mode)[-3:] == "600"


def test_a_world_readable_store_is_refused(store, tmp_path, authority):
    """Password hashes and authority cache remain owner-only."""
    store.add("a@example.com", GOOD)
    store.path.chmod(0o644)
    with pytest.raises(UserStoreError, match="insecure permissions"):
        UserStore(tmp_path / "users.json", **authority)


def test_a_corrupt_store_says_so_instead_of_starting_empty(store, tmp_path, authority):
    """Silently starting empty would look exactly like a wiped deployment."""
    store.add("a@example.com", GOOD)
    store.path.write_text(json.dumps({"users": [{"email": "a@example.com"}]}))
    store.path.chmod(0o600)
    with pytest.raises(UserStoreError, match="rollback"):
        UserStore(tmp_path / "users.json", **authority)


def test_redacted_never_leaks_the_hash_or_the_seed(store):
    user = store.add("a@example.com", GOOD)
    body = user.redacted()
    assert "password_hash" not in body
    assert "signing_seed" not in body
    assert body["did"] == user.did


def test_private_seed_never_enters_user_record(store):
    user = store.add("a@example.com", GOOD)
    assert not hasattr(user, "signing_seed")
    assert "signing_seed" not in store.path.read_text()
    assert user.signing_key_ref not in store.path.read_text()


def test_crash_between_anchor_advance_and_local_write_recovers(store, authority, tmp_path):
    store.add("a@example.com", GOOD)
    old = store.path.read_bytes()
    store.set_disabled("a@example.com", True)
    store.path.write_bytes(old)
    recovered = UserStore(tmp_path / "users.json", **authority)
    assert recovered.get("a@example.com").disabled
    assert recovered.path.read_bytes() != old


def test_older_rollback_is_refused(store, authority, tmp_path):
    store.add("a@example.com", GOOD)
    older = store.path.read_bytes()
    store.set_disabled("a@example.com", True)
    store.set_display_name("a@example.com", "New")
    store.path.write_bytes(older)
    with pytest.raises(UserStoreError, match="rollback"):
        UserStore(tmp_path / "users.json", **authority)


def test_stale_writer_cannot_erase_newer_roles(store, authority, tmp_path):
    store.add("a@example.com", GOOD)
    stale = UserStore(tmp_path / "users.json", **authority)
    store.set_roles("a@example.com", (OPERATOR,))
    with pytest.raises(RuntimeError, match="stale"):
        stale.set_roles("a@example.com", (VIEWER,))
    current = UserStore(tmp_path / "users.json", **authority)
    assert current.get("a@example.com").roles == (OPERATOR,)


def test_missing_anchor_refuses_existing_user_file(store, authority, tmp_path):
    store.add("a@example.com", GOOD)
    authority["anchor"].head = None
    with pytest.raises(UserStoreError, match="anchor missing"):
        UserStore(tmp_path / "users.json", **authority)


def test_retained_store_rechecks_disabled_account_and_anchor_outage(store, authority):
    store.add("a@example.com", GOOD)
    other = UserStore(store.path, **authority)
    other.set_disabled("a@example.com", True)
    assert store.verify("a@example.com", GOOD) is None
    anchor = authority["anchor"]
    anchor.latest = lambda: (_ for _ in ()).throw(RuntimeError("anchor offline"))
    with pytest.raises(RuntimeError, match="anchor offline"):
        store.verify("a@example.com", GOOD)


def test_audit_failure_does_not_publish_new_account(store, authority):
    audit = authority["audit_sink"]
    audit.fail = True
    with pytest.raises(RuntimeError, match="audit unavailable"):
        store.add("a@example.com", GOOD)
    audit.fail = False
    assert store.is_empty()
    assert authority["anchor"].latest() is None


def test_writer_holds_recovery_lock_through_local_replace(store, authority, tmp_path):
    store.add("a@example.com", GOOD)
    a = UserStore(store.path, **authority)
    entered = threading.Event()
    release = threading.Event()
    b_done = threading.Event()
    b_attempted = threading.Event()
    errors: list[Exception] = []
    original_write = a._write

    def delayed_write(payload: bytes) -> None:
        entered.set()
        if not release.wait(2):
            raise TimeoutError("writer release timed out")
        original_write(payload)

    a._write = delayed_write

    def writer_a() -> None:
        try:
            a.set_disabled("a@example.com", True)
        except Exception as exc:
            errors.append(exc)

    def writer_b() -> None:
        try:
            b_attempted.set()
            b = UserStore(tmp_path / "users.json", **authority)
            b.set_display_name("a@example.com", "current")
            b_done.set()
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=writer_a)
    second = threading.Thread(target=writer_b)
    first.start()
    assert entered.wait(2)
    second.start()
    assert b_attempted.wait(2)
    assert not b_done.wait(0.05)
    release.set()
    first.join(2)
    second.join(2)
    assert not errors
    assert b_done.is_set()
    current = UserStore(tmp_path / "users.json", **authority).get("a@example.com")
    assert current.disabled and current.display_name == "current"


def test_replaced_lock_cannot_let_delayed_writer_rollback_two_newer_heads(
    store, authority, tmp_path
):
    store.add("a@example.com", GOOD)
    delayed = UserStore(store.path, **authority)
    entered = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []
    original_write = delayed._write

    def late_write(payload: bytes) -> None:
        entered.set()
        if not release.wait(5):
            raise TimeoutError("writer release timed out")
        original_write(payload)

    delayed._write = late_write

    def writer() -> None:
        try:
            delayed.set_disabled("a@example.com", True)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    assert entered.wait(5)
    (tmp_path / ".users.lock").unlink()
    next_store = UserStore(store.path, **authority)
    next_store.set_display_name("a@example.com", "second")
    final_store = UserStore(store.path, **authority)
    final_store.set_display_name("a@example.com", "third")
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], UserStoreError)
    current = UserStore(store.path, **authority).get("a@example.com")
    assert current.disabled and current.display_name == "third"


def test_hardlinked_user_file_is_refused(store, authority, tmp_path):
    store.add("a@example.com", GOOD)
    (tmp_path / "other-link").hardlink_to(store.path)
    with pytest.raises(UserStoreError, match="file type"):
        UserStore(store.path, **authority)


def test_shared_store_keeps_both_successful_concurrent_field_edits(store, authority):
    store.add("a@example.com", GOOD)
    entered = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []
    original_save = store._save

    def delayed_save(candidate):
        if threading.current_thread().name == "display-writer":
            entered.set()
            if not release.wait(5):
                raise TimeoutError("writer release timed out")
        original_save(candidate)

    store._save = delayed_save

    def change_name() -> None:
        try:
            store.set_display_name("a@example.com", "New name")
        except Exception as exc:
            errors.append(exc)

    def change_pairing() -> None:
        try:
            store.set_pairing("a@example.com", "chat", "4242")
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=change_name, name="display-writer")
    second = threading.Thread(target=change_pairing, name="pairing-writer")
    first.start()
    assert entered.wait(5)
    second.start()
    second.join(0.2)
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    current = UserStore(store.path, **authority).get("a@example.com")
    assert current.display_name == "New name"
    assert current.pairings == {"chat": "4242"}


def test_shared_store_keeps_pinned_descriptors_private_to_active_call(store, tmp_path):
    store.add("a@example.com", GOOD)
    entered = threading.Event()
    release = threading.Event()
    reader_done = threading.Event()
    errors: list[Exception] = []
    original_write = store._write

    def delayed_write(payload: bytes) -> None:
        if threading.current_thread().name == "writer":
            entered.set()
            if not release.wait(5):
                raise TimeoutError("writer release timed out")
        original_write(payload)

    store._write = delayed_write

    def writer() -> None:
        try:
            store.set_disabled("a@example.com", True)
        except Exception as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            assert store.get("a@example.com").disabled
            reader_done.set()
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=writer, name="writer")
    second = threading.Thread(target=reader, name="reader")
    first.start()
    assert entered.wait(5)
    pinned_dir, pinned_lock = store._dir_fd, store._lock_fd
    (tmp_path / ".users.lock").unlink()
    second.start()
    assert not reader_done.wait(0.05)
    assert (store._dir_fd, store._lock_fd) == (pinned_dir, pinned_lock)
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], UserStoreError)
    assert reader_done.is_set()
