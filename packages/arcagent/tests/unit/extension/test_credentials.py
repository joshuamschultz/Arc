"""CredentialLifecycle — unattended renewal that cannot destroy a connection (COMP-011).

Covers REQ-287 (renew before expiry rather than waiting for a call to fail),
REQ-288 (at most one renewal in flight per connected account, persisted
atomically) and REQ-289 (terminal failure stops retrying, marks the connection,
and escalates through the operator path rather than agent chat).

The file's centre of gravity is ``test_only_one_renewal_is_ever_in_flight``.
Atlassian rotates its refresh token on every use, so a second renewal that
starts while the first is in flight presents a token the authorization server
has already consumed: the server answers ``invalid_grant``, and a late writer
can persist that rejected token over the working one — the connection is then
dead until a human re-consents.

[[feedback_concurrency_tests_must_interleave]]: that test does NOT hand the
renewal an instant mock. The first renewal parks inside the exchange on an
event, the test then drains the event loop so the second task runs as far as it
possibly can, and only then releases. Against a lifecycle with no single-writer
lock, the second task walks straight into the exchange and the entry count is
two. Against one that locks but does not re-check state after acquiring, the
second task performs a redundant exchange with the consumed token, the fake
server rejects it, and the escalation assertions fire. Both bugs are visible;
an instant mock would have shown neither, because ``gather`` would have run the
two coroutines end to end in sequence.

The fake stores ``await asyncio.sleep(0)`` on every method for the same reason:
a coroutine with no awaits in it never yields, which would let a buggy
implementation run to completion before the other task was ever scheduled.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.errors import ExtensionError
from arcagent.extension.credentials import (
    TERMINAL_ERROR_CODES,
    ConnectedAccount,
    CredentialLifecycle,
    CredentialMetadataStore,
    CredentialRenewalError,
    RenewedCredential,
)
from arcagent.extension.secrets import LocalFileSecretBackend, Secret, SecretStore
from arcagent.extension.state import ConnectionStateStore

if TYPE_CHECKING:
    from arcagent.extension.secrets import SecretRef

CALLER = "did:arc:agent:coder"
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
LIFETIME = timedelta(hours=1)


def _conformance(store: ConnectionStateStore) -> CredentialMetadataStore:
    """The real state store satisfies the Protocol this component consumes.

    Checked by ``mypy --strict``, so the seam cannot rot into a Protocol that
    only the test fake implements.
    """
    return store


class FakeStateStore:
    """Connection state, recording every patch so a test can search it."""

    def __init__(self, expires_at: datetime | None) -> None:
        self.record: dict[str, Any] = {
            "credential_expires_at": expires_at.isoformat() if expires_at else None,
            "credential_issuer": "https://auth.atlassian.com",
            "credential_audience": "api.atlassian.com",
            "health": "healthy",
        }
        self.patches: list[dict[str, Any]] = []
        self.metadata_write_fails = False

    async def get(self, agent: str, instance: str) -> Any:
        await asyncio.sleep(0)
        return _StateView(self.record)

    async def record_credential_metadata(
        self,
        agent: str,
        instance: str,
        *,
        expires_at: str | None = None,
        issuer: str | None = None,
        audience: str | None = None,
        last_refresh_at: str | None = None,
        actor_did: str,
    ) -> bool:
        await asyncio.sleep(0)
        if self.metadata_write_fails:
            raise OSError("the operational plane went away mid-renewal")
        patch = {
            "expires_at": expires_at,
            "issuer": issuer,
            "audience": audience,
            "last_refresh_at": last_refresh_at,
        }
        self.patches.append(patch)
        if expires_at is not None:
            self.record["credential_expires_at"] = expires_at
        return True

    async def set_health(self, agent: str, instance: str, health: str, *, actor_did: str) -> bool:
        await asyncio.sleep(0)
        self.patches.append({"health": health})
        self.record["health"] = health
        return True


class _StateView:
    """Read side of a connection record — credential coordinates only."""

    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    @property
    def credential_expires_at(self) -> str | None:
        value = self._record["credential_expires_at"]
        return str(value) if value is not None else None

    @property
    def credential_issuer(self) -> str | None:
        return str(self._record["credential_issuer"])

    @property
    def credential_audience(self) -> str | None:
        return str(self._record["credential_audience"])


class FakeEscalation:
    """The operator approval path — never agent chat."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def request_operator_attention(
        self, *, agent: str, instance: str, reason: str, detail: str
    ) -> None:
        await asyncio.sleep(0)
        self.calls.append(
            {"agent": agent, "instance": instance, "reason": reason, "detail": detail}
        )


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class RotatingTokenServer:
    """An authorization server whose refresh tokens are single use.

    Presenting a token it has already consumed answers ``invalid_grant`` — the
    behaviour that turns a concurrent renewal into a dead connection.
    """

    def __init__(self, *, first: str = "refresh-0") -> None:
        self.accepted = first
        self.issued = 0
        self.rejections: list[str] = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def exchange(self, presented: str) -> RenewedCredential:
        if presented != self.accepted:
            self.rejections.append(presented)
            raise CredentialRenewalError(
                error_code="invalid_grant", message="refresh token already used"
            )
        self.issued += 1
        self.accepted = f"refresh-{self.issued}"
        return RenewedCredential(
            value=self.accepted,
            issued_at=NOW,
            expires_at=NOW + LIFETIME,
            issuer="https://auth.atlassian.com",
            audience="api.atlassian.com",
        )


@pytest.fixture
def account() -> ConnectedAccount:
    return ConnectedAccount(agent="coder", instance="atlassian_work")


@pytest.fixture
def secrets(tmp_path: Path) -> SecretStore:
    return SecretStore(LocalFileSecretBackend(tmp_path / "coder" / "arc.env"))


def _lifecycle(
    secrets: SecretStore,
    state: FakeStateStore,
    escalation: FakeEscalation,
    *,
    now: datetime = NOW,
    sink: RecordingSink | None = None,
    sleeps: list[float] | None = None,
) -> CredentialLifecycle:
    async def record_sleep(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)
        await asyncio.sleep(0)

    return CredentialLifecycle(
        secrets=secrets,
        state=state,
        escalation=escalation,
        sink=sink,
        clock=lambda: now,
        sleep=record_sleep,
    )


async def _seed(secrets: SecretStore, ref: SecretRef, value: str) -> None:
    await secrets.put(ref, value, caller_did=CALLER)


# ---------------------------------------------------------------------------
# REQ-288 — one renewal in flight, and the persisted token is the accepted one
# ---------------------------------------------------------------------------


async def test_only_one_renewal_is_ever_in_flight(
    account: ConnectedAccount, secrets: SecretStore
) -> None:
    """Two concurrent renewals against a rotating single-use token (REQ-288)."""
    state = FakeStateStore(expires_at=NOW + timedelta(minutes=5))
    escalation = FakeEscalation()
    server = RotatingTokenServer()
    lifecycle = _lifecycle(secrets, state, escalation)
    await _seed(secrets, account.secret_ref, server.accepted)

    entered = asyncio.Event()
    release = asyncio.Event()
    presented: list[str] = []

    async def renew(current: Secret) -> RenewedCredential:
        presented.append(current.reveal())
        entered.set()
        await release.wait()
        return await server.exchange(current.reveal())

    tasks = [
        asyncio.create_task(lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER))
        for _ in range(2)
    ]
    await entered.wait()
    for _ in range(50):
        await asyncio.sleep(0)  # drain the loop: the second task runs as far as it can

    assert len(presented) == 1, "a second renewal entered while the first was in flight"

    release.set()
    outcomes = await asyncio.gather(*tasks)

    assert sorted(outcomes) == [False, True], "exactly one task should have renewed"
    assert len(presented) == 1, "the waiting task renewed again instead of re-reading state"
    assert server.rejections == [], "a consumed refresh token was presented"
    assert escalation.calls == []
    stored = await secrets.get(account.secret_ref, caller_did=CALLER)
    assert stored is not None
    assert stored.reveal() == server.accepted, "the persisted token is not the accepted one"


async def test_a_renewal_that_fails_to_record_metadata_still_keeps_the_new_token(
    account: ConnectedAccount, secrets: SecretStore
) -> None:
    """The value is persisted before the metadata, so a crash between them loses nothing.

    The reverse order throws away the only copy of the newly issued refresh
    token: the old one is already consumed, so the connection is unrecoverable.
    """
    state = FakeStateStore(expires_at=NOW + timedelta(minutes=5))
    server = RotatingTokenServer()
    lifecycle = _lifecycle(secrets, state, FakeEscalation())
    await _seed(secrets, account.secret_ref, server.accepted)

    async def renew(current: Secret) -> RenewedCredential:
        return await server.exchange(current.reveal())

    state.metadata_write_fails = True  # a crash between the two persists
    with pytest.raises(OSError, match="operational plane"):
        await lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER)

    stored = await secrets.get(account.secret_ref, caller_did=CALLER)
    assert stored is not None
    assert stored.reveal() == server.accepted


async def test_the_state_store_never_receives_a_credential_value(
    account: ConnectedAccount, secrets: SecretStore
) -> None:
    """Metadata only: expiry, issuer, audience — never the token (REQ-265)."""
    state = FakeStateStore(expires_at=NOW + timedelta(minutes=5))
    server = RotatingTokenServer()
    lifecycle = _lifecycle(secrets, state, FakeEscalation())
    await _seed(secrets, account.secret_ref, server.accepted)

    async def renew(current: Secret) -> RenewedCredential:
        return await server.exchange(current.reveal())

    await lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER)

    rendered = repr(state.patches)
    assert "refresh-0" not in rendered
    assert "refresh-1" not in rendered
    assert any(patch.get("expires_at") for patch in state.patches)


async def test_a_renewal_records_when_it_happened(
    account: ConnectedAccount, secrets: SecretStore
) -> None:
    """Last-refresh is a record of the renewal, stamped from the same clock."""
    state = FakeStateStore(expires_at=NOW + timedelta(minutes=5))
    server = RotatingTokenServer()
    lifecycle = _lifecycle(secrets, state, FakeEscalation())
    await _seed(secrets, account.secret_ref, server.accepted)

    async def renew(current: Secret) -> RenewedCredential:
        return await server.exchange(current.reveal())

    await lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER)

    (patch,) = [p for p in state.patches if p.get("last_refresh_at")]
    assert patch["last_refresh_at"] == NOW.isoformat()


# ---------------------------------------------------------------------------
# REQ-287 — proactive, never lazy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed_fraction,expected",
    [(0.0, False), (0.5, False), (0.74, False), (0.8, True), (1.0, True), (1.5, True)],
)
async def test_renewal_is_due_at_three_quarters_of_lifetime(
    account: ConnectedAccount, secrets: SecretStore, elapsed_fraction: float, expected: bool
) -> None:
    """Renewal is a function of elapsed lifetime, not of a call having failed."""
    issued = NOW - LIFETIME * elapsed_fraction
    state = FakeStateStore(expires_at=issued + LIFETIME)
    lifecycle = _lifecycle(secrets, state, FakeEscalation())
    await _seed(secrets, account.secret_ref, "refresh-0")

    renewed: list[str] = []

    async def renew(current: Secret) -> RenewedCredential:
        renewed.append(current.reveal())
        return RenewedCredential(value="refresh-1", issued_at=NOW, expires_at=NOW + LIFETIME)

    assert await lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER) is expected
    assert bool(renewed) is expected


async def test_renewal_happens_strictly_before_expiry(
    account: ConnectedAccount, secrets: SecretStore
) -> None:
    """A credential is never allowed to reach its expiry unrenewed (REQ-287)."""
    issued = NOW - LIFETIME * 0.8
    state = FakeStateStore(expires_at=issued + LIFETIME)
    lifecycle = _lifecycle(secrets, state, FakeEscalation())
    await _seed(secrets, account.secret_ref, "refresh-0")

    async def renew(current: Secret) -> RenewedCredential:
        return RenewedCredential(value="refresh-1", issued_at=NOW, expires_at=NOW + LIFETIME)

    assert await lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER) is True
    assert NOW < issued + LIFETIME, "the fixture must renew before expiry, not after"


async def test_an_account_with_no_known_expiry_is_left_alone(
    account: ConnectedAccount, secrets: SecretStore
) -> None:
    """A static API key has no lifetime; renewal must not invent one."""
    state = FakeStateStore(expires_at=None)
    lifecycle = _lifecycle(secrets, state, FakeEscalation())
    await _seed(secrets, account.secret_ref, "static-key")

    async def renew(current: Secret) -> RenewedCredential:
        raise AssertionError("a credential with no expiry must never be renewed")

    assert await lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER) is False


async def test_a_connection_that_was_never_authorized_escalates_rather_than_crashing(
    account: ConnectedAccount, secrets: SecretStore
) -> None:
    """There is nothing to renew and nothing an agent can do — ask the operator."""
    state = FakeStateStore(expires_at=NOW + timedelta(minutes=5))
    escalation = FakeEscalation()
    lifecycle = _lifecycle(secrets, state, escalation)

    async def renew(current: Secret) -> RenewedCredential:
        raise AssertionError("renewal must not run without a stored credential")

    with pytest.raises(ExtensionError) as excinfo:
        await lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER)

    assert excinfo.value.code == "CREDENTIAL_MISSING"
    assert state.record["health"] == "needs_attention"
    assert len(escalation.calls) == 1


def test_there_is_no_failure_triggered_renewal_entry_point() -> None:
    """REQ-287 is a shape, not just a schedule: no ``renew because a call 401'd`` hook."""
    surface = {name for name in dir(CredentialLifecycle) if not name.startswith("_")}
    forbidden = {name for name in surface if any(k in name for k in ("401", "unauthor", "fail"))}
    assert forbidden == set()


# ---------------------------------------------------------------------------
# REQ-289 — terminal versus transient, and the operator path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(TERMINAL_ERROR_CODES))
async def test_terminal_failure_stops_marks_and_escalates(
    account: ConnectedAccount, secrets: SecretStore, code: str
) -> None:
    """Re-consent is a human act: stop retrying, mark it, escalate (REQ-289)."""
    state = FakeStateStore(expires_at=NOW + timedelta(minutes=5))
    escalation = FakeEscalation()
    sleeps: list[float] = []
    lifecycle = _lifecycle(secrets, state, escalation, sleeps=sleeps)
    await _seed(secrets, account.secret_ref, "refresh-0")

    attempts: list[str] = []

    async def renew(current: Secret) -> RenewedCredential:
        attempts.append(current.reveal())
        raise CredentialRenewalError(error_code=code, message="the operator must re-consent")

    with pytest.raises(CredentialRenewalError):
        await lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER)

    assert len(attempts) == 1, "a terminal failure was retried"
    assert sleeps == [], "a terminal failure backed off instead of stopping"
    assert state.record["health"] == "needs_attention"
    assert len(escalation.calls) == 1
    assert escalation.calls[0]["instance"] == "atlassian_work"
    assert code in escalation.calls[0]["reason"]


async def test_a_transient_failure_retries_with_backoff_then_succeeds(
    account: ConnectedAccount, secrets: SecretStore
) -> None:
    """A blip is not a re-consent — retry, and do not bother the operator."""
    state = FakeStateStore(expires_at=NOW + timedelta(minutes=5))
    escalation = FakeEscalation()
    sleeps: list[float] = []
    lifecycle = _lifecycle(secrets, state, escalation, sleeps=sleeps)
    await _seed(secrets, account.secret_ref, "refresh-0")

    attempts: list[str] = []

    async def renew(current: Secret) -> RenewedCredential:
        attempts.append(current.reveal())
        if len(attempts) < 3:
            raise CredentialRenewalError(
                error_code="temporarily_unavailable", message="upstream is having a moment"
            )
        return RenewedCredential(value="refresh-1", issued_at=NOW, expires_at=NOW + LIFETIME)

    assert await lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER) is True
    assert len(attempts) == 3
    assert sleeps == sorted(sleeps) and len(sleeps) == 2, f"backoff was not increasing: {sleeps}"
    assert escalation.calls == []
    stored = await secrets.get(account.secret_ref, caller_did=CALLER)
    assert stored is not None
    assert stored.reveal() == "refresh-1"


async def test_exhausted_transient_retries_degrade_without_escalating(
    account: ConnectedAccount, secrets: SecretStore
) -> None:
    """An outage is not a consent problem: mark it degraded, do not page a human."""
    state = FakeStateStore(expires_at=NOW + timedelta(minutes=5))
    escalation = FakeEscalation()
    sleeps: list[float] = []
    lifecycle = _lifecycle(secrets, state, escalation, sleeps=sleeps)
    await _seed(secrets, account.secret_ref, "refresh-0")

    attempts: list[str] = []

    async def renew(current: Secret) -> RenewedCredential:
        attempts.append(current.reveal())
        raise CredentialRenewalError(error_code="server_error", message="502 from upstream")

    with pytest.raises(CredentialRenewalError):
        await lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER)

    assert len(attempts) > 1, "a transient failure was not retried"
    assert len(attempts) == len(sleeps) + 1, "retries and backoffs do not line up"
    assert state.record["health"] == "degraded"
    assert escalation.calls == []


async def test_a_failed_renewal_never_overwrites_the_working_credential(
    account: ConnectedAccount, secrets: SecretStore
) -> None:
    """The stored token still works; nothing rejected is allowed to replace it."""
    state = FakeStateStore(expires_at=NOW + timedelta(minutes=5))
    lifecycle = _lifecycle(secrets, state, FakeEscalation())
    await _seed(secrets, account.secret_ref, "refresh-0")

    async def renew(current: Secret) -> RenewedCredential:
        raise CredentialRenewalError(error_code="invalid_grant", message="already used")

    with pytest.raises(CredentialRenewalError):
        await lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER)

    stored = await secrets.get(account.secret_ref, caller_did=CALLER)
    assert stored is not None
    assert stored.reveal() == "refresh-0"


async def test_renewal_is_audited_without_the_token(
    account: ConnectedAccount, secrets: SecretStore
) -> None:
    state = FakeStateStore(expires_at=NOW + timedelta(minutes=5))
    sink = RecordingSink()
    lifecycle = _lifecycle(secrets, state, FakeEscalation(), sink=sink)
    await _seed(secrets, account.secret_ref, "refresh-0")

    async def renew(current: Secret) -> RenewedCredential:
        return RenewedCredential(value="refresh-1", issued_at=NOW, expires_at=NOW + LIFETIME)

    await lifecycle.ensure_fresh(account, renew=renew, caller_did=CALLER)

    renewals = [event for event in sink.events if event.action == "credential.renew"]
    assert len(renewals) == 1
    assert renewals[0].outcome == "allow"
    for event in sink.events:
        assert "refresh-1" not in event.model_dump_json()


async def test_separate_accounts_renew_independently(secrets: SecretStore) -> None:
    """The single-writer lock is per connected account, not a global bottleneck."""
    work = ConnectedAccount(agent="coder", instance="atlassian_work")
    personal = ConnectedAccount(agent="coder", instance="atlassian_personal")
    state = FakeStateStore(expires_at=NOW + timedelta(minutes=5))
    lifecycle = _lifecycle(secrets, state, FakeEscalation())
    await _seed(secrets, work.secret_ref, "refresh-work")
    await _seed(secrets, personal.secret_ref, "refresh-personal")

    inside = asyncio.Semaphore(0)
    both_arrived = asyncio.Barrier(2)

    async def renew(current: Secret) -> RenewedCredential:
        inside.release()
        await both_arrived.wait()  # deadlocks if one account's lock blocks the other
        return RenewedCredential(
            value=f"{current.reveal()}-next", issued_at=NOW, expires_at=NOW + LIFETIME
        )

    async with asyncio.timeout(5):
        outcomes = await asyncio.gather(
            lifecycle.ensure_fresh(work, renew=renew, caller_did=CALLER),
            lifecycle.ensure_fresh(personal, renew=renew, caller_did=CALLER),
        )

    assert list(outcomes) == [True, True]
