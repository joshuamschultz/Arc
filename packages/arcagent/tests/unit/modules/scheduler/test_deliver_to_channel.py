"""Canonical ``channel://<name>`` delivery for scheduled runs (alpha-2 item 48)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from arcagent.core.control_contract import (
    ControlArtifactUnavailableError,
    SignedControlRevision,
)
from arcagent.core.run_contract import CanonicalRunRequest
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.occurrence import canonical_definition
from arcagent.modules.scheduler.signed_dispatch import dispatch_signed_schedule


def _approved(entry: ScheduleEntry) -> ScheduleEntry:
    return entry.model_copy(
        update={
            "approval": SignedControlRevision(
                tenant_id="tenant",
                agent_did="did:arc:local:agent/one",
                purpose="schedule",
                artifact_id=entry.id,
                revision=1,
                definition_digest=hashlib.sha256(canonical_definition(entry)).hexdigest(),
                actor_did="did:arc:local:user/operator",
                issued_at=datetime.now(UTC),
                signature="ab" * 64,
            )
        }
    )


def _prepare(prompt: str, **kwargs: Any) -> CanonicalRunRequest:
    kwargs["purpose"] = kwargs.pop("run_purpose")
    return CanonicalRunRequest(input_text=prompt, **kwargs)


async def _issuer(request: CanonicalRunRequest, evidence: bytes) -> tuple[bytes, datetime]:
    del evidence
    return request.digest().encode(), datetime.now(UTC) + timedelta(minutes=1)


def _entry(deliver_to: str) -> ScheduleEntry:
    return _approved(
        ScheduleEntry(
            id="s1", type="interval", prompt="Digest", every_seconds=60, deliver_to=deliver_to
        )
    )


async def _dispatch(entry: ScheduleEntry, **extra: Any) -> Any:
    return await dispatch_signed_schedule(
        entry,
        now=datetime(2026, 9, 25, 12, 0, 2, tzinfo=UTC),
        default_timezone="UTC",
        tenant_id="tenant",
        agent_did="did:arc:local:agent/one",
        authority=AsyncMock(),
        issuer=_issuer,
        prepare=_prepare,
        run_fn=AsyncMock(return_value=SimpleNamespace(content="all clear", outcome_unknown=None)),
        **extra,
    )


def test_channel_uri_is_a_valid_deliver_to() -> None:
    entry = ScheduleEntry(
        id="s", type="interval", prompt="x", every_seconds=60, deliver_to="channel://ops"
    )
    assert entry.deliver_to == "channel://ops"


@pytest.mark.parametrize("bad", ["channel://", "channel://a b", "channel://a/b", "agent://x"])
def test_malformed_or_non_channel_uri_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        ScheduleEntry(id="s", type="interval", prompt="x", every_seconds=60, deliver_to=bad)


@pytest.mark.asyncio
async def test_channel_target_routes_to_team() -> None:
    sent: list[tuple[str, str]] = []

    async def team_send(target: str, text: str) -> None:
        sent.append((target, text))

    await _dispatch(_entry("channel://ops"), team_send=team_send)

    assert sent == [("channel://ops", "all clear")]


@pytest.mark.asyncio
async def test_platform_target_does_not_touch_the_team_bus() -> None:
    team_send = AsyncMock()
    await _dispatch(_entry("telegram:123"), team_send=team_send)
    team_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_target_without_a_team_sender_fails_closed() -> None:
    with pytest.raises(ControlArtifactUnavailableError):
        await _dispatch(_entry("channel://ops"))
