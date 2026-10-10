"""SealedAuthorityState: anchored, sealed, flock-protected authority persistence (SPEC-087 COMP-002).

Error family choice: ``AuthorityStateError`` subclasses ``arctrust.users.UserStoreError`` so every
existing ``except UserStoreError`` caller still catches persistence refusals after the extraction.

Real collaborators only: ``FileJournalAnchor`` signed by an ``InProcessSigner`` is the monotonic
anchor, files live on the real filesystem. The cipher (vault custody in production) and the audit
sinks are the external boundaries, so they are small test doubles; the cipher is a real
authenticated NaCl SecretBox so tampering is cryptographically detected, not simulated.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from nacl.secret import SecretBox

from arctrust import FileJournalAnchor, InProcessSigner, sealed_state
from arctrust.audit import AuditEvent
from arctrust.monotonic import AnchorHead, MonotonicAnchor
from arctrust.sealed_state import AuthorityStateError, SealedAuthorityState
from arctrust.users import UserStoreError

_ACTOR = "did:arc:test:user/sealed-state-actor"
_OPERATOR = InProcessSigner(bytes(range(32)))
_SECRET_MARKER = "do-not-store-this-in-plaintext"


# --- external-boundary doubles -------------------------------------------------


class SecretBoxCipher:
    """Authenticated ByteCipher standing in for vault custody."""

    def __init__(self, key: bytes = bytes(range(32))) -> None:
        self._box = SecretBox(key)

    def seal(self, payload: bytes) -> str:
        return base64.b64encode(bytes(self._box.encrypt(payload))).decode("ascii")

    def open(self, sealed: str) -> bytes:
        return self._box.decrypt(base64.b64decode(sealed, validate=True))


class BlockingCipher(SecretBoxCipher):
    """Pauses inside ``seal`` (which runs under the record lock) until released."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def seal(self, payload: bytes) -> str:
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("seal release timed out")
        return super().seal(payload)


@dataclass
class RecordingSink:
    events: list[AuditEvent] = field(default_factory=list)

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class StrictSink:
    events: list[AuditEvent] = field(default_factory=list)
    fail_on: str | None = None

    def write_durable(self, event: AuditEvent) -> None:
        if event.outcome == self.fail_on:
            raise OSError("injected durable append failure")
        self.events.append(event)


# --- fixtures ------------------------------------------------------------------


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _anchor(directory: Path, scope: str = "test/principals") -> FileJournalAnchor:
    return FileJournalAnchor(_private_dir(directory), scope=scope, signer=_OPERATOR)


@dataclass
class Rig:
    path: Path
    anchor: MonotonicAnchor
    cipher: SecretBoxCipher
    audit: RecordingSink
    strict: StrictSink | None
    record_name: str = "principals"

    def open(self, **overrides: Any) -> SealedAuthorityState:
        kwargs: dict[str, Any] = {
            "anchor": self.anchor,
            "cipher": self.cipher,
            "audit_sink": self.audit,
            "strict_audit_sink": self.strict,
            "record_name": self.record_name,
            "actor_did": _ACTOR,
        }
        kwargs.update(overrides)
        return SealedAuthorityState(self.path, **kwargs)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    state_dir = _private_dir(tmp_path / "state")
    return Rig(
        path=state_dir / "principals.json",
        anchor=_anchor(tmp_path / "anchor"),
        cipher=SecretBoxCipher(),
        audit=RecordingSink(),
        strict=StrictSink(),
    )


def _payload(revision: int) -> dict[str, Any]:
    return {
        "principals": [
            {"id": f"p-{revision}", "scopes": ["runs:read"], "nested": {"n": revision}}
        ],
        "secret": _SECRET_MARKER,
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- error family ----------------------------------------------------------------


def test_authority_state_error_is_caught_by_existing_user_store_handlers():
    assert issubclass(AuthorityStateError, UserStoreError)
    assert issubclass(AuthorityStateError, RuntimeError)


# --- round trip ------------------------------------------------------------------


def test_sealed_state_load_before_any_save_returns_none(rig):
    state = rig.open()
    assert state.load() is None
    assert not rig.path.exists()


def test_sealed_state_round_trip_returns_the_saved_payload(rig):
    state = rig.open()
    assert state.load() is None
    state.save(_payload(1))
    assert state.load() == _payload(1)


def test_sealed_state_survives_reopen_from_disk(rig):
    writer = rig.open()
    writer.load()
    writer.save(_payload(1))
    writer.save(_payload(2))
    assert rig.open().load() == _payload(2)


def test_sealed_state_file_is_sealed_owner_only_and_anchored(rig):
    state = rig.open()
    state.load()
    state.save(_payload(1))
    raw = rig.path.read_bytes()
    assert _SECRET_MARKER.encode() not in raw
    assert oct(rig.path.stat().st_mode)[-3:] == "600"
    head = rig.anchor.latest()
    assert head is not None and head.digest == _sha256(raw)


# --- integrity mismatch ----------------------------------------------------------


def test_sealed_state_tampered_ciphertext_is_refused(rig):
    state = rig.open()
    state.load()
    state.save(_payload(1))
    state.save(_payload(2))
    raw = bytearray(rig.path.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    rig.path.write_bytes(bytes(raw))
    with pytest.raises(AuthorityStateError, match=r"rollback|integrity"):
        rig.open().load()


def test_sealed_state_ciphertext_forged_under_a_new_digest_is_refused(rig):
    """A well-formed file sealed with the right key but never anchored is not authority."""
    state = rig.open()
    state.load()
    state.save(_payload(1))
    state.save(_payload(2))
    forged = rig.cipher.seal(b'{"principals":[],"secret":"attacker"}').encode("ascii")
    rig.path.write_bytes(forged)
    with pytest.raises(AuthorityStateError, match=r"rollback|integrity"):
        rig.open().load()


# --- recovery from a write intent -----------------------------------------------


def test_sealed_state_recovers_when_crash_left_the_previous_file(rig):
    """Anchor advanced, local replace never happened: the anchored intent is replayed."""
    state = rig.open()
    state.load()
    state.save(_payload(1))
    previous = rig.path.read_bytes()
    state.save(_payload(2))
    rig.path.write_bytes(previous)

    recovered = rig.open().load()

    assert recovered == _payload(2)
    assert rig.path.read_bytes() != previous
    assert _sha256(rig.path.read_bytes()) == rig.anchor.latest().digest


def test_sealed_state_recovers_when_crash_left_no_file_at_all(rig):
    state = rig.open()
    state.load()
    state.save(_payload(1))
    rig.path.unlink()

    assert rig.open().load() == _payload(1)
    assert rig.path.exists()


# --- unexplained rollback --------------------------------------------------------


def test_sealed_state_restored_older_file_is_refused_as_rollback(rig):
    """Two revisions back is not a crash window; it is a restore of revoked authority."""
    state = rig.open()
    state.load()
    state.save(_payload(1))
    older = rig.path.read_bytes()
    state.save(_payload(2))
    state.save(_payload(3))
    rig.path.write_bytes(older)
    with pytest.raises(AuthorityStateError, match="rollback"):
        rig.open().load()


def test_sealed_state_rollback_refusal_persists_across_every_read(rig):
    state = rig.open()
    state.load()
    state.save(_payload(1))
    older = rig.path.read_bytes()
    state.save(_payload(2))
    state.save(_payload(3))
    rig.path.write_bytes(older)
    reader = rig.open()
    for _ in range(2):
        with pytest.raises(AuthorityStateError, match="rollback"):
            reader.load()


# --- missing anchor --------------------------------------------------------------


def test_sealed_state_file_present_but_anchor_empty_is_refused(rig, tmp_path):
    state = rig.open()
    state.load()
    state.save(_payload(1))
    empty_anchor = _anchor(tmp_path / "fresh-anchor")
    with pytest.raises(AuthorityStateError, match="anchor missing"):
        rig.open(anchor=empty_anchor).load()


def test_sealed_state_anchor_from_another_scope_does_not_vouch_for_the_file(rig, tmp_path):
    state = rig.open()
    state.load()
    state.save(_payload(1))
    other = _anchor(tmp_path / "anchor", scope="test/other-record")
    with pytest.raises(AuthorityStateError, match="anchor missing"):
        rig.open(anchor=other).load()


# --- stale writers ---------------------------------------------------------------


def test_sealed_state_stale_writer_is_refused_and_newer_state_kept(rig):
    first = rig.open()
    first.load()
    first.save(_payload(1))
    stale = rig.open()
    stale.load()
    first.save(_payload(2))

    with pytest.raises(RuntimeError, match="stale"):
        stale.save(_payload(99))

    assert rig.open().load() == _payload(2)


def test_sealed_state_save_without_load_over_existing_record_is_refused(rig):
    writer = rig.open()
    writer.load()
    writer.save(_payload(1))
    blind = rig.open()
    with pytest.raises(RuntimeError, match="stale"):
        blind.save(_payload(99))
    assert rig.open().load() == _payload(1)


def test_sealed_state_concurrent_writers_serialize_and_loser_is_refused(rig):
    seed = rig.open()
    seed.load()
    seed.save(_payload(1))
    blocking = BlockingCipher()
    slow = rig.open(cipher=blocking)
    fast = rig.open()
    slow.load()
    fast.load()
    errors: list[BaseException] = []
    fast_done = threading.Event()

    def slow_writer() -> None:
        try:
            slow.save(_payload(2))
        except Exception as exc:
            errors.append(exc)

    def fast_writer() -> None:
        try:
            fast.save(_payload(3))
        except Exception as exc:
            errors.append(exc)
        finally:
            fast_done.set()

    first = threading.Thread(target=slow_writer)
    first.start()
    assert blocking.entered.wait(5)
    second = threading.Thread(target=fast_writer)
    second.start()
    # The fast writer must wait for the record lock the slow writer holds.
    assert not fast_done.wait(0.2)
    blocking.release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()

    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert "stale" in str(errors[0])
    assert rig.open().load() == _payload(2)


def test_sealed_state_two_records_in_one_directory_do_not_contend(rig, tmp_path):
    blocking = BlockingCipher()
    users = Rig(
        path=rig.path.parent / "users.json",
        anchor=_anchor(tmp_path / "users-anchor", scope="test/users"),
        cipher=blocking,
        audit=RecordingSink(),
        strict=StrictSink(),
        record_name="users",
    )
    held = users.open()
    held.load()
    errors: list[BaseException] = []

    def hold_users_lock() -> None:
        try:
            held.save({"users": []})
        except Exception as exc:
            errors.append(exc)

    holder = threading.Thread(target=hold_users_lock)
    holder.start()
    try:
        assert blocking.entered.wait(5)
        principals = rig.open()
        principals.load()
        principals.save(_payload(1))
        assert rig.open().load() == _payload(1)
    finally:
        blocking.release.set()
        holder.join(5)
    assert not errors
    assert users.open().load() == {"users": []}


# --- file hygiene ----------------------------------------------------------------


def test_sealed_state_world_readable_file_is_refused(rig):
    state = rig.open()
    state.load()
    state.save(_payload(1))
    rig.path.chmod(0o644)
    with pytest.raises(AuthorityStateError, match="insecure permissions"):
        rig.open().load()


def test_sealed_state_symlinked_file_is_refused(rig, tmp_path):
    state = rig.open()
    state.load()
    state.save(_payload(1))
    real = tmp_path / "elsewhere.json"
    real.write_bytes(rig.path.read_bytes())
    real.chmod(0o600)
    rig.path.unlink()
    rig.path.symlink_to(real)
    with pytest.raises(AuthorityStateError):
        rig.open().load()


def test_sealed_state_record_name_with_path_characters_is_refused(rig):
    for bad in ("../users", "a/b", "", ".hidden"):
        with pytest.raises(ValueError):
            rig.open(record_name=bad)


# --- audit -----------------------------------------------------------------------


def test_sealed_state_audits_change_and_read_under_the_record_name(rig):
    state = rig.open()
    state.load()
    state.save(_payload(1))
    digest = _sha256(rig.path.read_bytes())
    rig.open().load()

    changes = [(e.action, e.outcome, e.payload_hash) for e in rig.strict.events]
    assert changes == [
        ("principals.change", "attempt", digest),
        ("principals.change", "allow", digest),
    ]
    reads = [(e.action, e.outcome) for e in rig.audit.events]
    assert ("principals.read", "allow") in reads
    every = [*rig.strict.events, *rig.audit.events]
    assert {e.actor_did for e in every} == {_ACTOR}
    assert {e.target for e in every} == {"test/principals"}
    assert all(not e.action.startswith("users.") for e in every)


def test_sealed_state_audit_events_never_carry_the_payload(rig):
    state = rig.open()
    state.load()
    state.save(_payload(1))
    for event in [*rig.strict.events, *rig.audit.events]:
        assert _SECRET_MARKER not in event.model_dump_json()


def test_sealed_state_without_strict_sink_audits_changes_to_the_plain_sink(rig):
    state = rig.open(strict_audit_sink=None)
    state.load()
    state.save(_payload(1))
    outcomes = [(e.action, e.outcome) for e in rig.audit.events]
    assert ("principals.change", "attempt") in outcomes
    assert ("principals.change", "allow") in outcomes


def test_sealed_state_strict_attempt_audit_failure_refuses_the_save(rig):
    rig.strict.fail_on = "attempt"
    state = rig.open()
    state.load()
    with pytest.raises(OSError, match="durable append"):
        state.save(_payload(1))
    assert rig.anchor.latest() is None
    assert not rig.path.exists()
    rig.strict.fail_on = None
    assert rig.open().load() is None


def test_sealed_state_strict_commit_audit_failure_reports_uncertain_outcome(rig):
    rig.strict.fail_on = "allow"
    state = rig.open()
    state.load()
    with pytest.raises(AuthorityStateError, match="audit outcome is uncertain"):
        state.save(_payload(1))
    assert rig.anchor.latest() is not None
    rig.strict.fail_on = None
    assert rig.open().load() == _payload(1)


# --- compromised or lagging anchor (external custody boundary) -------------------


class ForeignScopeAnchor:
    """Claims one scope but serves heads journaled under another."""

    def __init__(self, real: FileJournalAnchor, claimed_scope: str) -> None:
        self._real = real
        self._claimed = claimed_scope

    @property
    def scope(self) -> str:
        return self._claimed

    def latest(self) -> AnchorHead | None:
        return self._real.latest()

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        return self._real.compare_and_advance(expected, digest, intent)


class LaggingAnchor:
    """Accepts an advance but keeps reporting the head from before it."""

    def __init__(self, real: FileJournalAnchor) -> None:
        self._real = real
        self._frozen: AnchorHead | None = None
        self.lag = False

    @property
    def scope(self) -> str:
        return self._real.scope

    def latest(self) -> AnchorHead | None:
        return self._frozen if self.lag else self._real.latest()

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        self._frozen = self._real.latest()
        return self._real.compare_and_advance(expected, digest, intent)


def _advance_raw(rig: Rig, plaintext: bytes, *, intent: str | None = None) -> None:
    """Anchor a revision directly, as a writer that crashed before its local write would."""
    sealed = rig.cipher.seal(plaintext)
    rig.anchor.compare_and_advance(
        rig.anchor.latest(),
        _sha256(sealed.encode("ascii")),
        sealed if intent is None else intent,
    )


def test_sealed_state_head_from_another_scope_is_refused(rig, tmp_path):
    foreign = _anchor(tmp_path / "foreign-anchor", scope="test/other-record")
    writer = rig.open(anchor=foreign)
    writer.load()
    writer.save(_payload(1))
    liar = ForeignScopeAnchor(foreign, claimed_scope="test/principals")
    with pytest.raises(AuthorityStateError, match="anchor scope mismatch"):
        rig.open(anchor=liar).load()


def test_sealed_state_anchor_that_does_not_report_the_advance_refuses_the_write(rig):
    lagging = LaggingAnchor(rig.anchor)
    state = rig.open(anchor=lagging)
    state.load()
    lagging.lag = True
    with pytest.raises(AuthorityStateError, match="advanced before local write"):
        state.save(_payload(1))
    assert not rig.path.exists()


# --- recovery-intent refusals ----------------------------------------------------


def test_sealed_state_anchored_non_object_record_is_refused(rig):
    _advance_raw(rig, b"[1,2,3]")
    with pytest.raises(AuthorityStateError, match="malformed"):
        rig.open().load()


def test_sealed_state_anchored_non_json_record_is_refused(rig):
    _advance_raw(rig, b"not json")
    with pytest.raises(AuthorityStateError, match="cannot read"):
        rig.open().load()


def test_sealed_state_missing_file_without_recovery_intent_is_refused(rig):
    _advance_raw(rig, b"{}", intent="")
    with pytest.raises(AuthorityStateError, match="recovery intent absent"):
        rig.open().load()
    assert not rig.path.exists()


def test_sealed_state_recovery_intent_that_does_not_match_the_digest_is_refused(rig):
    forged = rig.cipher.seal(b'{"secret":"attacker"}')
    _advance_raw(rig, b"{}", intent=forged)
    with pytest.raises(AuthorityStateError, match="recovery intent invalid"):
        rig.open().load()
    assert not rig.path.exists()


def test_sealed_state_recovery_write_that_did_not_land_is_refused(rig, monkeypatch):
    _advance_raw(rig, b"{}")
    monkeypatch.setattr(os, "replace", lambda *args, **kwargs: None)
    with pytest.raises(AuthorityStateError, match="recovery failed"):
        rig.open().load()


def test_sealed_state_recovery_that_leaves_the_old_file_is_refused(rig, monkeypatch):
    state = rig.open()
    state.load()
    state.save(_payload(1))
    previous = rig.path.read_bytes()
    state.save(_payload(2))
    rig.path.write_bytes(previous)
    monkeypatch.setattr(os, "replace", lambda *args, **kwargs: None)
    with pytest.raises(AuthorityStateError, match="integrity mismatch"):
        rig.open().load()


# --- write failure after the anchor advanced -----------------------------------


def test_sealed_state_failed_local_write_is_uncertain_and_recovered_on_next_load(rig):
    state = rig.open()
    state.load()
    rig.path.parent.chmod(0o500)
    try:
        with pytest.raises(AuthorityStateError, match="write outcome is uncertain"):
            state.save(_payload(1))
    finally:
        rig.path.parent.chmod(0o700)
    assert rig.anchor.latest() is not None
    assert rig.open().load() == _payload(1)


# --- lock and directory refusals -------------------------------------------------


def test_sealed_state_instance_busy_beyond_the_timeout_is_refused(rig, monkeypatch):
    monkeypatch.setattr(sealed_state, "_LOCK_TIMEOUT_SECONDS", 0.2)
    blocking = BlockingCipher()
    state = rig.open(cipher=blocking)
    state.load()
    holder = threading.Thread(target=lambda: state.save(_payload(1)))
    holder.start()
    try:
        assert blocking.entered.wait(5)
        with pytest.raises(AuthorityStateError, match="instance is busy"):
            state.load()
    finally:
        blocking.release.set()
        holder.join(5)


def test_sealed_state_record_lock_held_elsewhere_is_refused(rig, monkeypatch):
    monkeypatch.setattr(sealed_state, "_LOCK_TIMEOUT_SECONDS", 0.1)
    rig.open().load()
    lock_fd = os.open(rig.path.parent / ".principals.lock", os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        with pytest.raises(AuthorityStateError, match="lock is busy"):
            rig.open().load()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_sealed_state_symlinked_directory_is_refused(rig, tmp_path):
    link = tmp_path / "state-link"
    link.symlink_to(rig.path.parent)
    via_link = Rig(
        path=link / "principals.json",
        anchor=rig.anchor,
        cipher=rig.cipher,
        audit=rig.audit,
        strict=rig.strict,
    )
    with pytest.raises(AuthorityStateError, match="directory is a symlink"):
        via_link.open().load()


def test_sealed_state_group_readable_directory_is_refused(rig):
    rig.path.parent.chmod(0o750)
    with pytest.raises(AuthorityStateError, match="directory is not private"):
        rig.open().load()


def test_sealed_state_world_readable_lock_is_refused(rig):
    rig.open().load()
    (rig.path.parent / ".principals.lock").chmod(0o644)
    with pytest.raises(AuthorityStateError, match="lock is not private"):
        rig.open().load()


def _swap_directory_during_save(rig: Rig, swap: Callable[[Path], None]) -> list[BaseException]:
    """Pause a save under the record lock, run ``swap`` on its directory, then let it finish."""
    blocking = BlockingCipher()
    state = rig.open(cipher=blocking)
    state.load()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            state.save(_payload(1))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert blocking.entered.wait(5)
        swap(rig.path.parent)
    finally:
        blocking.release.set()
        thread.join(5)
    return errors


def test_sealed_state_directory_swapped_for_a_symlink_mid_save_is_refused(rig, tmp_path):
    moved = tmp_path / "state-moved"

    def to_symlink(directory: Path) -> None:
        directory.rename(moved)
        directory.symlink_to(moved)

    errors = _swap_directory_during_save(rig, to_symlink)
    assert len(errors) == 1 and isinstance(errors[0], AuthorityStateError)
    assert "directory changed" in str(errors[0])
    assert rig.anchor.latest() is None


def test_sealed_state_directory_replaced_mid_save_is_refused(rig, tmp_path):
    def to_new_directory(directory: Path) -> None:
        directory.rename(tmp_path / "state-moved")
        _private_dir(directory)

    errors = _swap_directory_during_save(rig, to_new_directory)
    assert len(errors) == 1 and isinstance(errors[0], AuthorityStateError)
    assert "directory changed" in str(errors[0])
    assert rig.anchor.latest() is None
