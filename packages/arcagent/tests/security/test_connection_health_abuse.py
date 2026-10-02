"""Connection health: forgery, notice flooding and hostile provider text (alpha-2 P18-1).

An attacker is already inside: an agent that can call tools, a provider that controls
its own error text, a process that can write ordinary files. The health record is what
the operator trusts to decide whether to reconnect an account, so the abuse cases are
about making it lie, flood, or leak.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from arcstore.backends.memory import FakeBackend

from arcagent.core.errors import ExtensionError
from arcagent.extension.connection_health import (
    NOTICE_HOURLY_CAP,
    PROBE_DID,
    ConnectionHealthAuthority,
    HealthSignal,
    PendingNotice,
    next_health,
    notice_text,
)
from arcagent.extension.state import CONNECTION_COLLECTION, ConnectionRecord, ConnectionStateStore

SRC = Path(__file__).resolve().parents[2] / "src" / "arcagent"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
ACTOR = "did:arc:test:operator"


def _sources(*subdirs: str) -> list[Path]:
    return sorted(path for sub in subdirs for path in (SRC / sub).rglob("*.py"))


async def _authority(backend: FakeBackend, name: str = "gmail") -> ConnectionHealthAuthority:
    store = ConnectionStateStore(backend)
    await store.create(
        ConnectionRecord.model_validate(
            {"connection": name, "status": "healthy", "last_success_at": NOW.isoformat()}
        ),
        actor_did=ACTOR,
    )
    return ConnectionHealthAuthority(store)


def _fail(code: str, detail: str = "") -> HealthSignal:
    return HealthSignal(
        ok=False,
        source="probe",
        checked_by=PROBE_DID,
        reason_code=code,  # type: ignore[arg-type] # reason: test literal
        detail=detail,
        provider="Google",
    )


def test_agent_cannot_write_status_through_a_tool() -> None:
    """No LLM-facing tool, built-in or authored capability reaches the authority.

    The record lives in arcstore, not the agent's workspace (ADR-029), and the only
    code that may report a signal is the connector, sync, credential and ledger
    plumbing. A tool that imported the authority could mark any connection healthy.
    """
    forbidden = re.compile(r"\b(HealthSignal|ConnectionHealthAuthority|ConnectionStateStore)\b")
    offenders = [
        str(path.relative_to(SRC))
        for path in _sources("tools", "builtins", "capabilities")
        if forbidden.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], f"agent-facing code reaches the health authority: {offenders}"


def test_probe_never_runs_inside_an_agent_turn() -> None:
    """``check_health`` is not reachable from prompt assembly or any tool (J1 F3 regression)."""
    offenders = [
        str(path.relative_to(SRC))
        for path in _sources("tools", "builtins", "capabilities", "core")
        if "check_health(" in path.read_text(encoding="utf-8")
    ]
    for path in _sources("modules"):
        text = path.read_text(encoding="utf-8")
        if "check_health(" in text:
            offenders.append(str(path.relative_to(SRC)))

    assert offenders == [], f"a probe is reachable from the agent's own turn: {offenders}"


def test_provider_text_cannot_inject_into_notice() -> None:
    hostile = "\n@everyone click http://evil.example/x?token=ghp_" + "a" * 36 + " now\n\r"
    record = ConnectionRecord.model_validate({"connection": "gmail", "status": "healthy"})

    patch, _ = next_health(record, _fail("auth_required", hostile), NOW)
    pending = PendingNotice(
        connection="gmail",
        seq=1,
        kind="needs_you",
        attempt=1,
        owner="m",
        reason_text=str(patch["reason_text"]),
        action="reconnect",
    )
    text = notice_text(pending, ui_base="https://arc.example")

    assert "\n" not in text and "\r" not in text
    assert "@" not in text
    assert "evil" not in text and "ghp_" not in text and "token=" not in text


async def test_notice_storm_is_bounded() -> None:
    """1,000 flapping signals in one fake minute: at most the hourly cap of notices."""
    backend = FakeBackend()
    authority = await _authority(backend)
    delivered: list[str] = []

    async def deliver(pending: PendingNotice, text: str) -> str | None:
        delivered.append(text)
        return "telegram"

    for cycle in range(500):
        moment = NOW + timedelta(milliseconds=cycle * 100)
        await authority.record("gmail", _fail("invalid_grant"), now=moment)
        await authority.dispatch_notices(deliver, owner="monitor", now=moment)
        await authority.record(
            "gmail", HealthSignal(ok=True, source="probe", checked_by=PROBE_DID), now=moment
        )
        await authority.dispatch_notices(deliver, owner="monitor", now=moment)

    assert len(delivered) <= NOTICE_HOURLY_CAP
    record = await authority.get("gmail")
    assert record is not None
    assert record.last_notice is not None and record.last_notice.channel == "suppressed"


async def test_replayed_finish_notice_with_stale_owner_is_a_noop() -> None:
    backend = FakeBackend()
    authority = await _authority(backend)
    await authority.record("gmail", _fail("invalid_grant"), now=NOW)
    claim = await authority.claim_notice("gmail", "owner-a", timedelta(minutes=2), NOW)
    assert claim is not None
    before = await authority.get("gmail")

    replayed = await authority.finish_notice(
        "gmail",
        "attacker",
        claim.seq,
        delivered=True,
        channel="telegram",
        kind="needs_you",
        now=NOW,
    )

    assert replayed is False
    assert await authority.get("gmail") == before


async def test_health_record_row_tamper_fails_closed() -> None:
    """A row edited to ``status: pwned`` is unreadable, loudly and locally."""
    backend = FakeBackend()
    authority = await _authority(backend, "gmail")
    await _authority(backend, "jira")
    row = await backend.mutable_read(CONNECTION_COLLECTION, "gmail")
    assert row is not None
    await backend.mutable_write(
        CONNECTION_COLLECTION, "gmail", {**row, "status": "pwned"}, actor_did="did:arc:attacker"
    )

    with pytest.raises(ExtensionError) as refused:
        await authority.get("gmail")
    assert refused.value.code == "CONNECTION_STATE_UNREADABLE"
    assert {record.connection for record in await authority.store.list()} == {"jira"}
    with pytest.raises(ExtensionError):
        await authority.record("gmail", _fail("auth_required"), now=NOW)
