"""Connection-health concurrency: CAS writes, probe claims and the notice lease (P18-1).

Every race here is FORCED. A test that lets ``gather`` run two instant coroutines
runs them one after the other and passes even when the code is unsafe
([[feedback_concurrency_tests_must_interleave]]), so the backend wrappers below
park both writers at a ``Barrier`` between the read and the write. Remove the CAS
guard from ``cas_update`` and these tests fail; that is the point of them.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from arcstore.backends.memory import FakeBackend

from arcagent.extension.connection_health import (
    NOTICE_CLAIM_TTL,
    ConnectionHealthAuthority,
    HealthSignal,
    PendingNotice,
)
from arcagent.extension.state import ConnectionRecord, ConnectionStateStore

ACTOR = "did:arc:test:operator"
SYSTEM = "did:arc:system:connection-health"
NOW = datetime.now(UTC).replace(microsecond=0)


class _ParkedBackend:
    """Delegates to a real backend, parking the first ``parties`` callers of one verb.

    Parking happens AFTER the real read returns, so every parked caller holds the
    same stale snapshot when it is released: exactly the read-then-write window a
    compare-and-set exists to close.
    """

    def __init__(self, inner: FakeBackend, verb: str, parties: int) -> None:
        self._inner = inner
        self._verb = verb
        self._barrier = asyncio.Barrier(parties)
        self._remaining = parties

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if name != self._verb:
            return target

        async def parked(*args: Any, **kwargs: Any) -> Any:
            result = await target(*args, **kwargs)
            if self._remaining > 0:
                self._remaining -= 1
                await self._barrier.wait()
            return result

        return parked


def _fail(code: str, *, checked_by: str = SYSTEM, source: str = "probe") -> HealthSignal:
    return HealthSignal(
        ok=False,
        source=source,  # type: ignore[arg-type] # reason: parametrised literal in tests
        checked_by=checked_by,
        reason_code=code,  # type: ignore[arg-type] # reason: parametrised literal in tests
        provider="Google",
    )


async def _seed(backend: Any, *names: str, status: str = "healthy") -> None:
    store = ConnectionStateStore(backend)
    for name in names:
        await store.create(
            ConnectionRecord.model_validate(
                {"connection": name, "status": status, "last_success_at": NOW.isoformat()}
            ),
            actor_did=ACTOR,
        )


async def test_two_writers_racing_lose_no_failure_count() -> None:
    inner = FakeBackend()
    await _seed(inner, "gmail")
    parked = _ParkedBackend(inner, "mutable_read", parties=2)
    first = ConnectionHealthAuthority(ConnectionStateStore(parked))
    second = ConnectionHealthAuthority(ConnectionStateStore(parked))

    await asyncio.gather(
        first.record("gmail", _fail("provider_unavailable"), now=NOW),
        second.record("gmail", _fail("provider_unavailable"), now=NOW),
    )

    record = await ConnectionStateStore(inner).get("gmail")
    assert record is not None
    assert record.consecutive_failures == 2
    assert record.revision == 2


async def test_two_processes_one_transition_one_notice_seq() -> None:
    inner = FakeBackend()
    await _seed(inner, "gmail")
    parked = _ParkedBackend(inner, "mutable_read", parties=2)
    first = ConnectionHealthAuthority(ConnectionStateStore(parked))
    second = ConnectionHealthAuthority(ConnectionStateStore(parked))

    results = await asyncio.gather(
        first.record("gmail", _fail("invalid_grant", checked_by="did:arc:agent:a"), now=NOW),
        second.record("gmail", _fail("invalid_grant", checked_by="did:arc:agent:b"), now=NOW),
    )

    record = await ConnectionStateStore(inner).get("gmail")
    assert record is not None
    assert (record.status, record.transition_seq, record.notice_seq) == ("needs_you", 1, 1)
    assert record.consecutive_failures == 2
    notices = [result.notice for result in results if result is not None]
    assert notices.count("needs_you") == 1


async def test_probe_claim_is_won_by_exactly_one_monitor() -> None:
    inner = FakeBackend()
    await _seed(inner, "gmail", "jira", "slack")
    parked = _ParkedBackend(inner, "mutable_query", parties=2)
    first = ConnectionHealthAuthority(ConnectionStateStore(parked), rng=lambda: 0.0)
    second = ConnectionHealthAuthority(ConnectionStateStore(parked), rng=lambda: 0.0)

    won_first, won_second = await asyncio.gather(
        first.claim_due_checks(NOW), second.claim_due_checks(NOW)
    )

    claimed = sorted(record.connection for record in [*won_first, *won_second])
    assert claimed == ["gmail", "jira", "slack"]


async def test_a_claimed_probe_is_not_due_again_inside_its_interval() -> None:
    inner = FakeBackend()
    await _seed(inner, "gmail")
    authority = ConnectionHealthAuthority(ConnectionStateStore(inner), rng=lambda: 0.0)

    assert [r.connection for r in await authority.claim_due_checks(NOW)] == ["gmail"]
    assert await authority.claim_due_checks(NOW + timedelta(minutes=29)) == []
    assert [
        r.connection for r in await authority.claim_due_checks(NOW + timedelta(minutes=30))
    ] == ["gmail"]


async def _needs_you(authority: ConnectionHealthAuthority, name: str = "gmail") -> None:
    await authority.record(name, _fail("invalid_grant"), now=NOW)


async def test_notice_claim_is_exclusive_and_expires() -> None:
    inner = FakeBackend()
    await _seed(inner, "gmail")
    authority = ConnectionHealthAuthority(ConnectionStateStore(inner))
    await _needs_you(authority)

    mine = await authority.claim_notice("gmail", "owner-a", NOTICE_CLAIM_TTL, NOW)
    theirs = await authority.claim_notice("gmail", "owner-b", NOTICE_CLAIM_TTL, NOW)
    later = NOW + NOTICE_CLAIM_TTL + timedelta(seconds=1)
    takeover = await authority.claim_notice("gmail", "owner-b", NOTICE_CLAIM_TTL, later)
    stale = await authority.finish_notice(
        "gmail", "owner-a", 1, delivered=True, channel="telegram", kind="needs_you", now=later
    )

    assert mine is not None
    assert theirs is None
    assert takeover is not None
    assert takeover.owner == "owner-b"
    assert stale is False
    record = await authority.get("gmail")
    assert record is not None
    assert record.notified_seq == 0
    assert record.notice_claim is not None
    assert record.notice_claim.owner == "owner-b"


async def test_concurrent_claims_have_one_winner() -> None:
    inner = FakeBackend()
    await _seed(inner, "gmail")
    await _needs_you(ConnectionHealthAuthority(ConnectionStateStore(inner)))
    parked = _ParkedBackend(inner, "mutable_read", parties=2)
    first = ConnectionHealthAuthority(ConnectionStateStore(parked))
    second = ConnectionHealthAuthority(ConnectionStateStore(parked))

    claims = await asyncio.gather(
        first.claim_notice("gmail", "a", NOTICE_CLAIM_TTL, NOW),
        second.claim_notice("gmail", "b", NOTICE_CLAIM_TTL, NOW),
    )

    assert sum(claim is not None for claim in claims) == 1


async def test_finish_with_stale_owner_is_a_noop() -> None:
    inner = FakeBackend()
    await _seed(inner, "gmail")
    authority = ConnectionHealthAuthority(ConnectionStateStore(inner))
    await _needs_you(authority)
    await authority.claim_notice("gmail", "owner-a", NOTICE_CLAIM_TTL, NOW)
    before = await authority.get("gmail")

    replayed = await authority.finish_notice(
        "gmail", "intruder", 1, delivered=True, channel="telegram", kind="needs_you", now=NOW
    )

    assert replayed is False
    assert await authority.get("gmail") == before


async def test_outage_that_recovers_before_delivery_sends_nothing() -> None:
    inner = FakeBackend()
    await _seed(inner, "gmail")
    authority = ConnectionHealthAuthority(ConnectionStateStore(inner))
    await _needs_you(authority)
    await authority.record(
        "gmail", HealthSignal(ok=True, source="sync", checked_by="did:arc:agent:a"), now=NOW
    )
    sent: list[str] = []

    async def deliver(pending: PendingNotice, text: str) -> str | None:
        sent.append(text)
        return "telegram"

    attempted = await authority.dispatch_notices(deliver, owner="monitor", now=NOW)

    record = await authority.get("gmail")
    assert record is not None
    assert (attempted, sent) == (0, [])
    assert record.transition_seq == 2
    assert record.notified_seq == 2


async def test_delivery_gives_up_after_three_attempts() -> None:
    inner = FakeBackend()
    await _seed(inner, "gmail")
    authority = ConnectionHealthAuthority(ConnectionStateStore(inner))
    await _needs_you(authority)
    calls: list[datetime] = []

    async def never(pending: PendingNotice, text: str) -> str | None:
        calls.append(NOW)
        return None

    schedule = [
        timedelta(0),  # attempt 1
        timedelta(seconds=30),  # inside the 1 min backoff: not claimable
        timedelta(minutes=1, seconds=1),  # attempt 2
        timedelta(minutes=4),  # inside the 5 min backoff
        timedelta(minutes=6, seconds=2),  # attempt 3 -> gives up
        timedelta(hours=1),  # nothing left to try
    ]
    for offset in schedule:
        await authority.dispatch_notices(never, owner="monitor", now=NOW + offset)

    record = await authority.get("gmail")
    assert record is not None
    assert len(calls) == 3
    assert record.notified_seq == 1
    assert record.last_notice is not None
    assert record.last_notice.delivered is False
    assert record.notice_claim is None


async def test_notice_for_an_outage_that_mends_mid_delivery_owes_a_recovered_line() -> None:
    inner = FakeBackend()
    await _seed(inner, "gmail")
    authority = ConnectionHealthAuthority(ConnectionStateStore(inner))
    await _needs_you(authority)
    pending = await authority.claim_notice("gmail", "monitor", NOTICE_CLAIM_TTL, NOW)
    assert pending is not None
    await authority.record(
        "gmail", HealthSignal(ok=True, source="sync", checked_by="did:arc:agent:a"), now=NOW
    )
    await authority.finish_notice(
        "gmail",
        "monitor",
        pending.seq,
        delivered=True,
        channel="telegram",
        kind="needs_you",
        now=NOW,
    )
    texts: list[str] = []

    async def deliver(notice: PendingNotice, text: str) -> str | None:
        texts.append(text)
        return "telegram"

    await authority.dispatch_notices(deliver, owner="monitor", now=NOW)

    assert texts == ["Connection 'gmail' is working again."]


async def test_a_flapping_connection_is_capped_per_hour() -> None:
    from arcagent.extension.connection_health import NOTICE_HOURLY_CAP

    inner = FakeBackend()
    await _seed(inner, "gmail")
    authority = ConnectionHealthAuthority(ConnectionStateStore(inner))
    delivered: list[str] = []

    async def deliver(notice: PendingNotice, text: str) -> str | None:
        delivered.append(text)
        return "telegram"

    for cycle in range(40):
        moment = NOW + timedelta(seconds=cycle * 2)
        await authority.record("gmail", _fail("invalid_grant"), now=moment)
        await authority.dispatch_notices(deliver, owner="monitor", now=moment)
        await authority.record(
            "gmail", HealthSignal(ok=True, source="probe", checked_by=SYSTEM), now=moment
        )
        await authority.dispatch_notices(deliver, owner="monitor", now=moment)

    assert len(delivered) <= NOTICE_HOURLY_CAP
    record = await authority.get("gmail")
    assert record is not None
    assert record.last_notice is not None
