"""``arc approve`` — the on-box operator surface mints a gate-acceptable grant.

Proves the full round-trip: a pending row resolved by the CLI carries an
operator-signed grant that (a) verifies against the matching call and (b) is
signed by the deployment operator the agent's gate pins to.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.sqlite import SqliteBackend
from arcstore.config import store_db_path
from arctrust.policy import (
    OperatorApprovalAuthority,
    ToolCall,
    _hash_call,
    grant_from_wire,
    verify_approval,
)

from arccli.commands.approve import approve_handler

_AGENT = "did:arc:test:exec/agent1"


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))


def _call() -> ToolCall:
    return ToolCall(
        tool_name="send_message", arguments={"to": "coder_agent"}, agent_did=_AGENT,
        session_id="", classification="unclassified",
    )


async def _seed_pending(call_hash: str, *, enriched: bool = False) -> None:
    backend = SqliteBackend(store_db_path(None))
    await backend.start()
    extra: dict[str, object] = {}
    if enriched:
        extra = {
            "session_id": "sess-1",
            "arguments": {"to": "coder_agent", "body": "hello"},
            "provenance": [{"legs": ["private_data"], "tool": "file_read", "args": "p", "at": "t"}],
        }
    try:
        await ApprovalStore(backend).create(
            PendingApproval(
                id="req1", agent_did=_AGENT, agent_label="josh_agent",
                tool="send_message", legs=["external_comms", "private_data"], call_hash=call_hash,
                **extra,
            )
        )
    finally:
        await backend.stop()


async def _read(pid: str) -> PendingApproval | None:
    backend = SqliteBackend(store_db_path(None))
    await backend.start()
    try:
        return await ApprovalStore(backend).get(pid)
    finally:
        await backend.stop()


def test_approve_mints_grant_that_verifies_against_the_call() -> None:
    call = _call()
    asyncio.run(_seed_pending(_hash_call(call)))

    approve_handler(["req1"])  # the operator action

    row = asyncio.run(_read("req1"))
    assert row is not None
    assert row.status == "approved"
    assert row.grant is not None
    grant = grant_from_wire(row.grant)
    # The grant unlocks exactly this call...
    assert verify_approval(call, grant) is True
    # ...and is signed by the on-box deployment operator (the DID the gate pins to).
    from arccli.commands.operator import resolve_operator_signer

    expected_did = OperatorApprovalAuthority(resolve_operator_signer()).did
    assert grant.approver_did == expected_did


def test_list_displays_enrichment_context(capsys: pytest.CaptureFixture[str]) -> None:
    # SPEC-035 approval enrichment — the operator sees session, redacted args, and
    # leg provenance when listing pending requests.
    asyncio.run(_seed_pending(_hash_call(_call()), enriched=True))

    approve_handler(["list"])

    out = capsys.readouterr().out
    assert "sess-1" in out
    assert "to: coder_agent" in out
    assert "file_read" in out


def test_resolve_displays_context_before_approving(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `arc approve <id>` prints the triage context (what + why) before signing.
    asyncio.run(_seed_pending(_hash_call(_call()), enriched=True))

    approve_handler(["req1"])

    out = capsys.readouterr().out
    assert "arguments:" in out
    assert "leg provenance" in out
    assert "Approved req1" in out


def test_deny_marks_denied_without_a_grant() -> None:
    call = _call()
    asyncio.run(_seed_pending(_hash_call(call)))

    approve_handler(["req1", "--deny"])

    row = asyncio.run(_read("req1"))
    assert row is not None
    assert row.status == "denied"
    assert row.grant is None
