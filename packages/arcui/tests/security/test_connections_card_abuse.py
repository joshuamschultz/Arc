"""The connections card: forged health, leaked credentials, tampered rows (alpha-2 P18-1).

A card is read by every viewer and its answer decides whether an operator reconnects
an account, so the abuse cases are about making it lie to someone, or tell them
something it must not.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import arcagent
import pytest
from arctrust.secrets import SECRET_PATTERNS
from packages.arcui.tests.connection_fleet import INSTANCE, SENTINEL, Fleet


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fleet:
    return Fleet(tmp_path, monkeypatch)


def _record(fleet: Fleet) -> arcagent.ConnectionRecord:
    record = asyncio.run(arcagent.ConnectionStateStore(fleet.backend).get(INSTANCE))
    assert record is not None
    return record


def test_forged_healthy_signal_from_a_viewer_is_refused(fleet: Fleet) -> None:
    fleet.install()
    fleet.report(ok=False)
    before = _record(fleet)

    forged = fleet.client.post(
        f"/api/connections/{INSTANCE}/probe",
        json={"status": "healthy", "revision": 999, "action": "none"},
        headers=fleet.headers("viewer"),
    )

    assert forged.status_code == 403
    assert _record(fleet) == before, "a refused request must not touch the record"


def test_no_route_accepts_a_client_supplied_status(fleet: Fleet) -> None:
    """The only thing that moves the record is a real check; the body is never believed."""
    fleet.install()
    before = _record(fleet)

    hostile = fleet.client.post(
        f"/api/connections/{INSTANCE}/probe",
        json={"status": "pwned", "revision": 999, "reason_text": "<script>x</script>"},
        headers=fleet.headers("operator"),
    )

    assert hostile.status_code == 200
    after = _record(fleet)
    assert after.status == "healthy", "derived from the real probe, not from the body"
    assert after.revision < before.revision + 5, "a client cannot set the CAS token"
    assert "script" not in (after.reason_text or "")


def test_card_read_reveals_no_credential_or_account_secret(fleet: Fleet) -> None:
    fleet.install()
    hostile = "Bearer ghp_" + "a" * 36 + " https://x.example/cb?code=SECRETCODE&state=y"
    fleet.report(ok=False, code="provider_unavailable")  # counted: below the ceiling, no text
    asyncio.run(
        arcagent.ConnectionHealthAuthority(arcagent.ConnectionStateStore(fleet.backend)).record(
            INSTANCE,
            arcagent.HealthSignal(
                ok=False,
                source="operator",
                checked_by=arcagent.PROBE_DID,
                reason_code="provider_unavailable",
                detail=hostile,
                provider="Acme",
            ),
        )
    )

    bodies = [
        fleet.client.get("/api/connections", headers=fleet.headers("viewer")).text,
        fleet.client.get("/api/agents/acme/connectors", headers=fleet.headers("viewer")).text,
    ]

    for body in bodies:
        assert SENTINEL not in body, "the stored credential leaked into the card"
        assert "SECRETCODE" not in body
        for name, pattern in SECRET_PATTERNS:
            assert pattern.search(body) is None, f"{name} shape found in a card response"
        json.loads(body)


def test_health_record_row_tamper_fails_closed_and_stays_local(fleet: Fleet) -> None:
    """One corrupted row reads ``unknown`` for that connection; the page still renders."""
    fleet.install()
    row = asyncio.run(fleet.backend.mutable_read("connections", INSTANCE))
    assert row is not None
    asyncio.run(
        fleet.backend.mutable_write(
            "connections", INSTANCE, {**row, "status": "pwned"}, actor_did="did:arc:attacker"
        )
    )

    response = fleet.client.get("/api/connections", headers=fleet.headers("viewer"))

    assert response.status_code == 200
    (card,) = response.json()["connections"]
    assert (card["status"], card["display_status"]) == ("unknown", "unknown")
    assert card["action"] == "none"
