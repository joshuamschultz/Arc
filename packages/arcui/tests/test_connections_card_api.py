"""The connections card API: one truthful status from stored facts, no probing (P18-1, 7.1).

``GET /api/connections`` used to be a list of grants and the card asked a second
question per page view ("is it working?") by reading every declared credential three
times and running the vendor CLI. Every one of those reads was a signed WORM row
attributed to the operator. The card now reads one health record and the durable sync
rows per connection, so a page view makes zero credential reads and spawns nothing.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import arcagent
import pytest
from packages.arcui.tests.connection_fleet import INSTANCE as _INSTANCE
from packages.arcui.tests.connection_fleet import Fleet as _Fleet


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Fleet:
    return _Fleet(tmp_path, monkeypatch)


def test_row_carries_status_and_knowledge_sync(fleet: _Fleet) -> None:
    fleet.install()
    fleet.report(ok=False)
    fleet.sync(fleet.did, _INSTANCE, ttl=60)
    fleet.sync("did:arc:other:agent", f"{_INSTANCE}:inbox", ttl=60)

    (row,) = fleet.listing()

    assert row["status"] == "needs_you"
    assert row["display_status"] == "needs_you", "a person-needed state is never hidden by a sync"
    assert (row["action"], row["action_label"]) == ("reconnect", "Reconnect Acme")
    assert row["reason_text"] == "Acme sign-in expired or was revoked"
    assert row["connect_kind"] == "token"
    syncs = {item["source_id"]: item for item in row["knowledge_sync"]}
    assert set(syncs) == {_INSTANCE, f"{_INSTANCE}:inbox"}
    assert syncs[_INSTANCE]["agent"] == "acme", "the roster names the agent"
    assert syncs[f"{_INSTANCE}:inbox"]["agent"] == "did:arc:other:agent"
    assert all(item["running"] for item in syncs.values())


def test_display_status_is_syncing_only_with_a_live_lease(fleet: _Fleet) -> None:
    fleet.install()
    fleet.sync(fleet.did, _INSTANCE, ttl=0.01)
    asyncio.run(asyncio.sleep(0.05))

    (stale,) = fleet.listing()
    assert stale["knowledge_sync"][0]["state"] == "running"
    assert stale["knowledge_sync"][0]["running"] is False
    assert stale["display_status"] == "healthy", "an expired lease is a crash, not a sync"

    fleet.sync(fleet.did, _INSTANCE, ttl=60)
    (live,) = fleet.listing()
    assert live["display_status"] == "syncing"
    assert live["status"] == "healthy", "syncing is derived, never stored"


def test_card_read_makes_zero_secret_reads_and_zero_cli_spawns(
    fleet: _Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """J1 G13: a page view is a database read."""
    fleet.install()
    fleet.sink.events.clear()

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a card read must not spawn a process")

    async def refuse_async(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a card read must not spawn a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse_async)
    monkeypatch.setattr(subprocess, "Popen", refuse)

    for _ in range(10):
        fleet.listing()
    for _ in range(3):
        response = fleet.client.get("/api/agents/acme/connectors", headers=fleet.headers("viewer"))
        assert response.status_code == 200

    assert [e for e in fleet.sink.events if e.action == "secret.read"] == []


def test_agent_connectors_needs_attention_comes_from_the_record(fleet: _Fleet) -> None:
    fleet.install()

    def attention() -> bool:
        body = fleet.client.get("/api/agents/acme/connectors", headers=fleet.headers("viewer"))
        (row,) = body.json()["instances"]
        return bool(row["needs_attention"])

    assert attention() is False
    fleet.report(ok=False)
    assert attention() is True
    fleet.report(ok=True)
    assert attention() is False


def test_probe_route_persists_and_returns_the_record(fleet: _Fleet) -> None:
    fleet.install()
    before = asyncio.run(arcagent.ConnectionStateStore(fleet.backend).get(_INSTANCE))
    assert before is not None

    response = fleet.client.post(
        f"/api/connections/{_INSTANCE}/probe", headers=fleet.headers("operator")
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["reachable"]) == ("healthy", True)
    assert [tool["name"] for tool in body["tools"]] == ["ping"]
    after = asyncio.run(arcagent.ConnectionStateStore(fleet.backend).get(_INSTANCE))
    assert after is not None
    assert after.last_checked_at is not None
    assert after.last_checked_at != before.last_checked_at
    viewer = fleet.client.post(
        f"/api/connections/{_INSTANCE}/probe", headers=fleet.headers("viewer")
    )
    assert viewer.status_code == 403


def test_a_reported_failure_shows_its_reason_in_plain_words(fleet: _Fleet) -> None:
    fleet.install()

    fleet.report(ok=False, code="token_revoked")

    (row,) = fleet.listing()
    assert (row["status"], row["reason_code"]) == ("needs_you", "token_revoked")
    assert row["reason_text"] == "Acme access was revoked"
