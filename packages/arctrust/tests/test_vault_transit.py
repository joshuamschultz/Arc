"""VaultTransit behaviour beyond the shared contract: auth lifecycle, TLS, limits, audit."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from packages.arctrust.tests.vault_fake import FakeVault
from pydantic import ValidationError

from arctrust.audit import AuditEvent
from arctrust.signer import ECDSA_P256, SignerError, verify_signature
from arctrust.transit_cipher import TransitDecryptError, TransitUnavailableError
from arctrust.vault_transit import (
    VaultTransit,
    VaultTransitConfig,
    VaultTransitUnavailableError,
)

AAD = b"conn-1\x00refresh_token"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ListSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def fake() -> Iterator[FakeVault]:
    with FakeVault() as vault:
        yield vault


def _config(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch, **overrides: Any
) -> VaultTransitConfig:
    monkeypatch.setenv("ARC_TEST_SECRET_ID", fake.state.secret_id)
    values: dict[str, Any] = {
        "addr": fake.addr,
        "ca_bundle": str(fake.ca_bundle),
        "role_id": fake.state.role_id,
        "secret_source": "env:ARC_TEST_SECRET_ID",
        "max_retries": 2,
        "timeout_s": 2,
    }
    values.update(overrides)
    return VaultTransitConfig(**values)


def _transit(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> tuple[VaultTransit, Clock, list[float]]:
    clock, sleeps = Clock(), []
    overrides = kwargs.pop("config", {})
    transit = VaultTransit(
        _config(fake, monkeypatch, **overrides),
        clock=clock,
        sleep=sleeps.append,
        **kwargs,
    )
    return transit, clock, sleeps


# -- config: TLS is mandatory ---------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    ["http://vault:8200", "https://user:pw@vault:8200", "https://vault:8200/v1", "vault:8200"],
)
def test_config_requires_a_bare_https_origin(addr: str) -> None:
    with pytest.raises(ValidationError):
        VaultTransitConfig(addr=addr, ca_bundle="/ca.pem", role_id="r", secret_source="env:X")


def test_config_requires_a_ca_bundle() -> None:
    with pytest.raises(ValidationError):
        VaultTransitConfig(addr="https://vault:8200", role_id="r", secret_source="env:X")  # type: ignore[call-arg]  # reason: proving the field is required


def test_config_requires_complete_auth() -> None:
    with pytest.raises(ValidationError):
        VaultTransitConfig(addr="https://vault:8200", ca_bundle="/ca.pem", role_id="r")
    with pytest.raises(ValidationError):
        VaultTransitConfig(addr="https://v:8200", ca_bundle="/ca.pem", auth_method="kubernetes")


def test_a_server_signed_by_another_ca_is_unavailable(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    with FakeVault() as other:
        transit, _, _ = _transit(fake, monkeypatch, config={"ca_bundle": str(other.ca_bundle)})
        with pytest.raises(VaultTransitUnavailableError):
            transit.encrypt("connector-credentials", b"v", aad=AAD)
    assert fake.calls("/login") == 0


# -- auth: AppRole, namespace, renewal, re-login ----------------------------------


def test_env_secret_is_removed_once_read(fake: FakeVault, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    transit, _, _ = _transit(fake, monkeypatch)
    assert "ARC_TEST_SECRET_ID" not in os.environ
    assert (
        transit.decrypt(
            "connector-credentials",
            transit.encrypt("connector-credentials", b"x", aad=AAD),
            aad=AAD,
        )
        == b"x"
    )


def test_namespace_header_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    with FakeVault(namespace="arc/prod") as fake:
        bare, _, _ = _transit(fake, monkeypatch, config={"max_retries": 0})
        with pytest.raises(VaultTransitUnavailableError):
            bare.encrypt("connector-credentials", b"v", aad=AAD)
        scoped, _, _ = _transit(fake, monkeypatch, config={"namespace": "arc/prod"})
        assert scoped.encrypt("connector-credentials", b"v", aad=AAD).startswith("vault:v1:")


def test_token_is_renewed_before_it_expires(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit, clock, _ = _transit(fake, monkeypatch)
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    clock.now += 41  # past two-thirds of the 60s TTL
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    assert fake.calls("/renew-self") == 1
    assert fake.calls("/login") == 1


def test_refused_renewal_logs_in_again(fake: FakeVault, monkeypatch: pytest.MonkeyPatch) -> None:
    transit, clock, _ = _transit(fake, monkeypatch)
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    fake.state.renew_fails = True
    clock.now += 41
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    assert fake.calls("/renew-self") == 1
    assert fake.calls("/login") == 2


def test_renewal_clipped_by_max_ttl_logs_in_again(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.state.max_ttl = 10
    transit, clock, _ = _transit(fake, monkeypatch)
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    clock.now += 41
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    assert fake.calls("/login") == 2


def test_expired_token_past_its_deadline_logs_in_again(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit, clock, _ = _transit(fake, monkeypatch)
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    clock.now += 120
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    assert fake.calls("/renew-self") == 0
    assert fake.calls("/login") == 2


def test_revoked_token_is_reminted_once(fake: FakeVault, monkeypatch: pytest.MonkeyPatch) -> None:
    transit, _, _ = _transit(fake, monkeypatch)
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    fake.state.tokens.clear()
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    assert fake.calls("/login") == 2


def test_wrong_secret_id_is_unavailable(fake: FakeVault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_WRONG", "not-the-secret")
    transit, _, sleeps = _transit(fake, monkeypatch, config={"secret_source": "env:ARC_WRONG"})
    monkeypatch.setenv("ARC_TEST_SECRET_ID", "unused")
    with pytest.raises(VaultTransitUnavailableError):
        transit.sign("operator", b"m")
    assert len(sleeps) == 2  # bounded: max_retries, then fail closed


def test_close_revokes_the_token(fake: FakeVault, monkeypatch: pytest.MonkeyPatch) -> None:
    transit, _, _ = _transit(fake, monkeypatch)
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    transit.close()
    assert fake.calls("/revoke-self") == 1
    assert fake.state.tokens == {}


# -- limits: timeout, bounded retry, circuit breaker -------------------------------


def test_timeout_is_bounded_and_fails_closed(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit, _, sleeps = _transit(fake, monkeypatch, config={"timeout_s": 0.3})
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    fake.state.delay_s = 1.0
    with pytest.raises(VaultTransitUnavailableError):
        transit.encrypt("connector-credentials", b"v", aad=AAD)
    assert fake.calls("/encrypt/") == 1 + 3  # one success, then 1 + max_retries
    assert sleeps == [0.1, 0.2]


def test_server_errors_are_retried_then_succeed(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit, _, sleeps = _transit(fake, monkeypatch)
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    fake.state.fail_statuses = [503, 502]
    assert transit.encrypt("connector-credentials", b"v", aad=AAD).startswith("vault:")
    assert len(sleeps) == 2


def test_permission_denied_is_not_retried(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit, _, sleeps = _transit(fake, monkeypatch)
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    fake.state.fail_statuses = [404]
    with pytest.raises(VaultTransitUnavailableError):
        transit.encrypt("connector-credentials", b"v", aad=AAD)
    assert sleeps == []


def test_breaker_opens_then_probes_after_cooldown(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit, clock, _ = _transit(
        fake,
        monkeypatch,
        config={"max_retries": 0, "breaker_failures": 2, "breaker_cooldown_s": 30},
    )
    transit.encrypt("connector-credentials", b"v", aad=AAD)
    fake.state.fail_statuses = [503, 503]
    for _ in range(2):
        with pytest.raises(VaultTransitUnavailableError):
            transit.encrypt("connector-credentials", b"v", aad=AAD)
    before = fake.calls("/encrypt/")
    with pytest.raises(VaultTransitUnavailableError, match="circuit is open"):
        transit.encrypt("connector-credentials", b"v", aad=AAD)
    assert fake.calls("/encrypt/") == before  # fail fast, Vault untouched
    clock.now += 31
    assert transit.encrypt("connector-credentials", b"v", aad=AAD).startswith("vault:")


def test_unavailable_is_both_cipher_and_signer_failure() -> None:
    assert issubclass(VaultTransitUnavailableError, TransitUnavailableError)
    assert issubclass(VaultTransitUnavailableError, SignerError)


# -- keys ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag", ["exportable", "allow_plaintext_backup", "deletion_allowed", "derived"]
)
def test_an_unsafe_key_is_refused_before_use(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    fake.add_key("arc-connector-credentials", "aes256-gcm96", **{flag: True})
    transit, _, _ = _transit(fake, monkeypatch)
    with pytest.raises(VaultTransitUnavailableError, match="non-exportable"):
        transit.encrypt("connector-credentials", b"v", aad=AAD)
    assert fake.calls("/encrypt/") == 0


def test_a_signing_key_of_the_wrong_type_is_refused(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit = VaultTransit(_config(fake, monkeypatch), algorithm=ECDSA_P256)
    with pytest.raises(VaultTransitUnavailableError):
        transit.public_key("operator")  # arc-operator is ed25519


def test_unmapped_key_reference_never_reaches_vault(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit, _, _ = _transit(fake, monkeypatch)
    with pytest.raises(VaultTransitUnavailableError):
        transit.encrypt("arc-operator", b"v", aad=AAD)
    assert fake.calls("/login") == 0


def test_ecdsa_signatures_are_always_low_s(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit = VaultTransit(
        _config(fake, monkeypatch, keys={"operator": "arc-operator-p256"}), algorithm=ECDSA_P256
    )
    public = transit.public_key("operator")
    for index in range(24):
        message = f"head-{index}".encode()
        signature = transit.sign("operator", message)
        assert verify_signature(ECDSA_P256, message, signature, public)
        assert transit.verify("operator", message, signature)


def test_a_rotated_signing_key_is_refused_until_repinned(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit, _, _ = _transit(fake, monkeypatch)
    transit.public_key("operator")
    fake.state.sign_version = 2
    with pytest.raises(SignerError, match="rotated"):
        transit.sign("operator", b"m")


def test_verify_rejects_a_forged_signature(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit, _, _ = _transit(fake, monkeypatch)
    signature = transit.sign("operator", b"m")
    assert transit.verify("operator", b"m", signature)
    assert not transit.verify("operator", b"m", bytes(64))
    assert not transit.verify("operator", b"other", signature)


def test_aad_swap_is_a_decrypt_error_not_an_outage(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    transit, _, _ = _transit(fake, monkeypatch, config={"breaker_failures": 1})
    sealed = transit.encrypt("connector-credentials", b"v", aad=AAD)
    for _ in range(3):
        with pytest.raises(TransitDecryptError):
            transit.decrypt("connector-credentials", sealed, aad=b"conn-2\x00refresh_token")
    assert transit.decrypt("connector-credentials", sealed, aad=AAD) == b"v"  # breaker closed


# -- audit: one event per call, no material ----------------------------------------


def test_every_call_is_audited_without_material(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sink = ListSink()
    transit, _, _ = _transit(fake, monkeypatch, audit_sink=sink)
    plaintext = b"ya29.refresh-token-PLAINTEXT"
    sealed = transit.encrypt("connector-credentials", plaintext, aad=AAD)
    transit.decrypt("connector-credentials", sealed, aad=AAD)
    signature = transit.sign("operator", b"head")
    transit.verify("operator", b"head", signature)
    with pytest.raises(TransitDecryptError):
        transit.decrypt("connector-credentials", sealed, aad=b"other")
    actions = [event.action for event in sink.events]
    for action in ("login", "key_read", "encrypt", "decrypt", "sign", "verify"):
        assert f"custody.transit.{action}" in actions
    assert [e.outcome for e in sink.events if e.action == "custody.transit.decrypt"] == [
        "allow",
        "refused",
    ]
    dumped = json.dumps([e.model_dump(mode="json") for e in sink.events]) + caplog.text
    tokens = list(fake.state.tokens)
    for secret in (
        plaintext.decode(),
        base64.b64encode(plaintext).decode(),
        sealed,
        fake.state.secret_id,
        base64.b64encode(signature).decode(),
        *tokens,
    ):
        assert secret not in dumped


def test_default_sink_is_the_structured_log(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="arctrust.vault_transit")
    transit, _, _ = _transit(fake, monkeypatch)
    transit.encrypt("connector-credentials", b"secret-plaintext", aad=AAD)
    assert "custody.transit.encrypt" in caplog.text
    assert "secret-plaintext" not in caplog.text


def test_secret_source_never_appears_in_errors(
    fake: FakeVault, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake.state.fail_statuses = [500] * 10
    transit, _, _ = _transit(fake, monkeypatch, config={"max_retries": 1})
    with pytest.raises(VaultTransitUnavailableError) as raised:
        transit.encrypt("connector-credentials", b"v", aad=AAD)
    assert fake.state.secret_id not in str(raised.value)
    assert fake.addr not in str(raised.value)
