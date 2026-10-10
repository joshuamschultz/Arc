"""The account authority composed from machine config, against a fake Vault over TLS."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from arctrust.paths import config_file
from arctrust.vault_lease import VaultLeaseError
from packages.arccli.tests.accounts_support import (
    Deployment,
    RecordingSink,
    enrolled_deployment,
)

from arccli.commands._accounts import (
    AccountsConfigError,
    _AuthorityFactory,
    _ProcessActor,
    build_user_store_factory,
)

PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Own Arc home per test, even in the cross-package battery (no arccli conftest)."""
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.delenv("ARCSTORE_DATA_DIR", raising=False)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with enrolled_deployment(tmp_path, monkeypatch) as enrolled:
        yield enrolled


def _factory(deployment: Deployment, sink: RecordingSink | None = None):
    factory = build_user_store_factory(sink or RecordingSink(), users_path=deployment.users_path)
    assert factory is not None
    return factory


def test_enrolled_deployment_creates_and_reads_accounts_through_vault(deployment):
    sink = RecordingSink()
    factory = _factory(deployment, sink)

    created = factory().add("ada@example.com", PASSWORD, roles=("operator",))

    assert created.is_operator
    reread = factory().get("ada@example.com")
    assert reread is not None and reread.did == created.did
    # Each capability logged in with its own AppRole; nothing shared.
    assert {"issuer", "cipher", "anchor", "config-reader"} <= {
        name.rsplit("-", 1)[-1] if not name.endswith("config-reader") else "config-reader"
        for name in deployment.vault.logins
    }
    assert any(event.action == "users.change" for event in sink.events)


def test_the_authority_is_opened_once_and_shared(deployment):
    factory = _factory(deployment)
    factory()
    logins = len(deployment.vault.logins)
    factory()
    assert len(deployment.vault.logins) == logins


def test_the_first_account_consumes_the_single_use_bootstrap_marker(deployment):
    marker = deployment.grants_file.with_name("users-bootstrap.once")
    assert marker.exists()
    _factory(deployment)().add("ada@example.com", PASSWORD, roles=("operator",))
    assert not marker.exists()


def test_no_accounts_block_means_no_factory_and_one_secret_free_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    config_file("arcagent.toml").parent.mkdir(parents=True, exist_ok=True)
    config_file("arcagent.toml").write_text('[security]\ntier = "personal"\n')
    with caplog.at_level(logging.WARNING):
        assert build_user_store_factory(RecordingSink()) is None
    messages = [r.getMessage() for r in caplog.records if "accounts" in r.getMessage()]
    assert len(messages) == 1
    assert "secret" not in messages[0].lower()


def test_an_invalid_accounts_block_is_a_typed_error(tmp_path: Path):
    config_file("arcagent.toml").parent.mkdir(parents=True, exist_ok=True)
    config_file("arcagent.toml").write_text(
        '[security.accounts]\nvault_url = "http://insecure.example"\n'
    )
    with pytest.raises(AccountsConfigError):
        build_user_store_factory(RecordingSink())


def test_a_secret_value_in_the_config_is_refused(deployment):
    path = config_file("arcagent.toml")
    path.write_text(path.read_text().replace("file:", "", 1))
    with pytest.raises(AccountsConfigError):
        build_user_store_factory(RecordingSink())


def test_a_ca_that_is_not_the_pinned_one_is_refused(deployment):
    # Valid TLS trust, different bytes: only the pin in the signed config catches it.
    ca = deployment.vault.cert_path
    ca.write_bytes(ca.read_bytes() + b"\n")
    with pytest.raises(VaultLeaseError):
        _factory(deployment)()


def test_an_expired_grant_is_refused(deployment, monkeypatch: pytest.MonkeyPatch):
    later = SimpleNamespace(time=lambda: time.time() + 400 * 86_400, monotonic=time.monotonic)
    monkeypatch.setattr("arctrust.vault_lease.time", later)
    with pytest.raises(VaultLeaseError):
        _factory(deployment)()


def test_a_token_with_the_wrong_policy_is_refused(deployment):
    deployment.vault.policy_override["arc-acme-dgx-cipher"] = ["arc-acme-dgx-issuer"]
    with pytest.raises(VaultLeaseError):
        _factory(deployment)()


def test_a_token_with_extra_policies_is_refused(deployment):
    deployment.vault.policy_override["arc-acme-dgx-issuer"] = ["arc-acme-dgx-issuer", "default"]
    with pytest.raises(VaultLeaseError):
        _factory(deployment)()


def test_a_wrong_approle_secret_is_refused(deployment):
    secret_file = deployment.home / "issuer.secret"
    secret_file.write_text("not-the-secret")
    secret_file.chmod(0o600)
    with pytest.raises(VaultLeaseError):
        _factory(deployment)()


def test_a_failed_durable_audit_append_refuses_the_mutation(deployment):
    sink = RecordingSink()
    factory = _factory(deployment, sink)
    store = factory()
    sink.fail = True
    with pytest.raises(Exception, match=r"disk full|audit"):
        store.add("ada@example.com", PASSWORD)
    sink.fail = False
    assert factory().get("ada@example.com") is None


def test_a_plain_sink_that_cannot_append_durably_is_refused(deployment):
    class PlainSink:
        def write(self, event: object) -> None:
            return None

    factory = build_user_store_factory(PlainSink(), users_path=deployment.users_path)
    assert factory is not None
    with pytest.raises(AccountsConfigError):
        factory().add("ada@example.com", PASSWORD)


def test_only_the_process_held_proof_authenticates():
    proof = object()
    actor = _ProcessActor(proof, "did:arc:acme:operator/abcd1234")
    assert actor.authenticate(proof, action="users.mutate") == "did:arc:acme:operator/abcd1234"
    with pytest.raises(PermissionError):
        actor.authenticate(object(), action="users.mutate")
    with pytest.raises(PermissionError):
        actor.authenticate("did:arc:acme:operator/abcd1234", action="users.mutate")


def test_a_lease_error_reopens_the_authority_once():
    class Stale:
        closed = False

        def user_store(self, _proof: object) -> object:
            raise VaultLeaseError("lease gone")

        def close(self) -> None:
            self.closed = True

    class Fresh:
        def user_store(self, _proof: object) -> str:
            return "store"

    stale, fresh = Stale(), Fresh()
    factory = _AuthorityFactory(SimpleNamespace(), RecordingSink(), None)
    factory._authority = stale  # type: ignore[assignment]  # reason: stub stands in for the authority
    factory._open = lambda: fresh  # type: ignore[method-assign,assignment,return-value]  # reason: stub
    assert factory() == "store"
    assert stale.closed
    assert factory._authority is fresh


def test_local_config_errors_are_authority_errors_so_arcui_answers_503() -> None:
    from arctrust.authority import AccountAuthorityError

    from arccli.commands._accounts import AccountsConfigError

    assert issubclass(AccountsConfigError, AccountAuthorityError)


def _app_principal():
    import uuid
    from datetime import UTC, datetime, timedelta

    from arctrust.principals import Principal, Scope, derive_app_did
    from nacl.signing import SigningKey

    public_key = bytes(SigningKey.generate().verify_key)
    now = datetime.now(UTC)
    return Principal(
        id=uuid.uuid4().hex,
        name="billing-bot",
        did=derive_app_did(public_key, org="acme"),
        public_key=public_key.hex(),
        secret_digest="ab" * 32,
        scopes={Scope.RUNS_READ: ("did:arc:acme:agent/olivia",)},
        subjects=("did:arc:acme:user/ada",),
        status="pending",
        created_by="did:arc:acme:user/ada",
        approved_by=None,
        created_at=now,
        expires_at=now + timedelta(days=30),
        last_used_at=None,
        revoked_by=None,
        revoked_at=None,
    )


def test_the_factory_opens_a_principal_store_through_vault_beside_users(deployment):
    """principals() mirrors the users store: same authority, own sealed record, durable audit."""
    from arctrust.principals import PrincipalStore

    sink = RecordingSink()
    factory = _factory(deployment, sink)
    factory().add("ada@example.com", PASSWORD, roles=("operator",))

    store = factory.principals()

    assert isinstance(store, PrincipalStore)
    principal = _app_principal()
    store.request(principal)
    reread = factory.principals().get(principal.id)
    assert reread is not None and reread.status == "pending"
    # Its own record: the users store is untouched and still readable.
    assert factory().get("ada@example.com") is not None
    [event] = [e for e in sink.events if e.action == "principal.request"]
    assert event.actor_did.startswith("did:arc:acme:operator/")
    assert principal.secret_digest not in event.model_dump_json()


def test_the_authority_binds_principal_stores_only_to_the_process_proof(deployment):
    from arctrust.authority import AccountAuthorityError
    from arctrust.principals import PrincipalStore

    factory = _factory(deployment)
    authority = factory._current()

    assert isinstance(authority.principal_store(factory._proof), PrincipalStore)
    with pytest.raises(AccountAuthorityError):
        authority.principal_store(object())
    with pytest.raises(AccountAuthorityError):
        authority.principal_store("did:arc:acme:operator/forged")
