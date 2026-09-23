"""Account mutation outcomes are tied to strict audit durability."""

from __future__ import annotations

import pytest

from arctrust.users import UserStore, UserStoreError
from packages.arctrust.tests.test_users import FakeAnchor, FakeAudit, FakeCipher, FakeIssuer, GOOD


class StrictAudit:
    def __init__(self):
        self.events = []
        self.fail_on = None

    def write_durable(self, event):
        if event.outcome == self.fail_on:
            raise OSError("injected durable append failure")
        self.events.append(event)


def _store(tmp_path, strict, *, anchor=None, issuer=None, cipher=None):
    return UserStore(
        tmp_path / "users.json", issuer=issuer or FakeIssuer(),
        anchor=anchor or FakeAnchor(), cipher=cipher or FakeCipher(),
        audit_sink=FakeAudit(), strict_audit_sink=strict,
        actor_did="did:arc:test:user/authenticated-actor",
    )


def test_attempt_audit_failure_prevents_anchor_advance(tmp_path):
    strict = StrictAudit()
    strict.fail_on = "attempt"
    anchor = FakeAnchor()
    store = _store(tmp_path, strict, anchor=anchor)
    with pytest.raises(OSError):
        store.add("a@example.com", GOOD)
    assert anchor.latest() is None
    assert not store.path.exists()


def test_commit_audit_failure_reports_uncertain_and_restart_recovers(tmp_path):
    strict = StrictAudit()
    strict.fail_on = "allow"
    anchor, issuer, cipher = FakeAnchor(), FakeIssuer(), FakeCipher()
    store = _store(tmp_path, strict, anchor=anchor, issuer=issuer, cipher=cipher)
    with pytest.raises(UserStoreError, match="committed; audit outcome is uncertain"):
        store.add("a@example.com", GOOD)
    assert anchor.latest() is not None
    reopened = _store(tmp_path, StrictAudit(), anchor=anchor, issuer=issuer, cipher=cipher)
    assert reopened.get("a@example.com") is not None
    with pytest.raises(ValueError, match="already exists"):
        reopened.add("a@example.com", GOOD)


def test_account_actor_is_recorded_on_mutation(tmp_path):
    strict = StrictAudit()
    store = _store(tmp_path, strict)
    store.add("a@example.com", GOOD)
    assert {event.actor_did for event in strict.events} == {
        "did:arc:test:user/authenticated-actor"
    }
