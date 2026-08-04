"""The user store is what makes an approval attributable to a person."""

from __future__ import annotations

import json

import pytest

from arctrust.users import OPERATOR, VIEWER, UserStore, UserStoreError

GOOD = "correct-horse-battery"


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "users.json")


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
    assert a.signing_seed != b.signing_seed


def test_the_password_is_never_stored(store):
    store.add("a@example.com", GOOD)
    raw = (store.path).read_text()
    assert GOOD not in raw
    assert "$argon2id$" in raw


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


def test_a_telegram_account_resolves_to_its_user(store):
    """Password reset has to find the person from the channel they message from."""
    store.add("a@example.com", GOOD, telegram_user_id="4242")
    assert store.by_telegram("4242").email == "a@example.com"
    assert store.by_telegram("9999") is None


def test_users_survive_a_reload(store, tmp_path):
    store.add("a@example.com", GOOD, roles=(OPERATOR, VIEWER), telegram_user_id="4242")
    reopened = UserStore(tmp_path / "users.json")
    user = reopened.get("a@example.com")
    assert user.telegram_user_id == "4242"
    assert set(user.roles) == {OPERATOR, VIEWER}
    assert reopened.verify("a@example.com", GOOD) is not None


def test_the_store_is_written_owner_only(store):
    store.add("a@example.com", GOOD)
    assert oct(store.path.stat().st_mode)[-3:] == "600"


def test_a_world_readable_store_is_refused(store, tmp_path):
    """It holds password hashes and signing seeds; loose permissions fail closed."""
    store.add("a@example.com", GOOD)
    store.path.chmod(0o644)
    with pytest.raises(UserStoreError, match="insecure permissions"):
        UserStore(tmp_path / "users.json")


def test_a_corrupt_store_says_so_instead_of_starting_empty(store, tmp_path):
    """Silently starting empty would look exactly like a wiped deployment."""
    store.add("a@example.com", GOOD)
    store.path.write_text(json.dumps({"users": [{"email": "a@example.com"}]}))
    store.path.chmod(0o600)
    with pytest.raises(UserStoreError, match="malformed"):
        UserStore(tmp_path / "users.json")


def test_redacted_never_leaks_the_hash_or_the_seed(store):
    user = store.add("a@example.com", GOOD)
    body = user.redacted()
    assert "password_hash" not in body
    assert "signing_seed" not in body
    assert body["did"] == user.did
