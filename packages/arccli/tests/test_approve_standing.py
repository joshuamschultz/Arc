"""``arc approve <id> --always`` / ``arc approve list`` / ``arc approve revoke <grant>``.

The CLI twin of arcui's "Always allow" and Standing approvals panel (SPEC-035
OQ-3, ruled 2026-10-03): the same ``approve_always`` operation, the same store.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.memory import FakeBackend
from arcstore.standing_grants import StandingGrantStore
from arctrust.policy import INTERACTIVE_ORIGIN, scenario_grant_from_wire

from arccli.commands.approve import approve_handler

_AGENT = "did:arc:local:executor/c0bef560"


@pytest.fixture(autouse=True)
def _backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeBackend:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    backend = FakeBackend()
    monkeypatch.setattr("arccli.commands.approve._backend_factory", lambda: backend)
    return backend


def _seed(backend: FakeBackend, *, eligible: bool = True) -> None:
    async def _run() -> None:
        await backend.start()
        await ApprovalStore(backend).create(
            PendingApproval(
                id="req1",
                agent_did=_AGENT,
                agent_label="Olivia",
                tool="dropbox_upload",
                legs=["external_comms", "private_data", "untrusted_input"],
                call_hash="abc",
                destination="personal_dropbox",
                grant_tool="dropbox_upload",
                standing_eligible=eligible,
            )
        )

    asyncio.run(_run())


def _standing(backend: FakeBackend) -> StandingGrantStore:
    return StandingGrantStore(backend)


def test_always_approves_and_stores_a_standing_grant(
    _backend: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(_backend)

    approve_handler(["req1", "--always"])

    out = capsys.readouterr().out
    assert "Always allowed" in out
    assert asyncio.run(ApprovalStore(_backend).get("req1")).status == "approved"  # type: ignore[union-attr]
    [row] = asyncio.run(_standing(_backend).active_for(_AGENT))
    assert scenario_grant_from_wire(row.grant).origin == INTERACTIVE_ORIGIN
    assert row.id in out


def test_always_is_refused_for_an_ineligible_request(_backend: FakeBackend) -> None:
    _seed(_backend, eligible=False)

    with pytest.raises(SystemExit):
        approve_handler(["req1", "--always"])

    assert asyncio.run(ApprovalStore(_backend).get("req1")).status == "pending"  # type: ignore[union-attr]
    assert asyncio.run(_standing(_backend).list()) == []


def test_list_shows_standing_approvals(
    _backend: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(_backend)
    approve_handler(["req1", "--always"])
    capsys.readouterr()

    approve_handler(["list"])

    out = capsys.readouterr().out
    assert "Standing approvals" in out
    assert "personal_dropbox" in out
    assert "Olivia" in out


def test_revoke_takes_effect_at_once(
    _backend: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(_backend)
    approve_handler(["req1", "--always"])
    [row] = asyncio.run(_standing(_backend).active_for(_AGENT))

    approve_handler(["revoke", row.id])

    assert "Revoked" in capsys.readouterr().out
    assert asyncio.run(_standing(_backend).active_for(_AGENT)) == []
    with pytest.raises(SystemExit):
        approve_handler(["revoke", row.id])
