"""Pulse checks run only from an operator-approved revision (P47 / item 61 part 3).

Drives the production ``LocalControlArtifactAuthority`` and the real engine, so
the approval path is the one an operator uses, not a stand-in.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import arctrust
import pytest

from arcagent.core.control_contract import ControlArtifactRefusedError
from arcagent.core.run_contract import CanonicalRunRequest
from arcagent.modules.pulse.approval import approve_pulse_check, pulse_status
from arcagent.modules.pulse.config import PulseConfig
from arcagent.modules.pulse.engine import PulseEngine, parse_pulse_file
from arcagent.modules.pulse.signed_dispatch import canonical_definition

TENANT = "tenant"
AGENT_DID = "did:arc:local:agent/one"
OPERATOR_DID = "did:arc:operator:approver/test"
PULSE = (
    "## health\n- **Interval:** 5 min\n- **Action:** Check health\n"
    "## inbox\n- **Interval:** 10 min\n- **Action:** Sweep inbox\n"
)


def _digest(definition: bytes) -> str:
    return hashlib.sha256(definition).hexdigest()


def _prepare(prompt: str, **kwargs: Any) -> CanonicalRunRequest:
    kwargs["purpose"] = kwargs.pop("run_purpose")
    return CanonicalRunRequest(input_text=prompt, **kwargs)


class Deployment:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        signer = arctrust.InProcessSigner(os.urandom(32))
        self.authority = arctrust.LocalControlArtifactAuthority(
            workspace / "control", signer=signer, operator_did=OPERATOR_DID
        )
        self.issuer = arctrust.LocalRunTriggerIssuer(signer)
        self.pulse = workspace / "pulse.md"
        self.pulse.write_text(PULSE, encoding="utf-8")
        self.run_fn = AsyncMock(return_value=None)

    def _check(self, name: str) -> Any:
        return next(c for c in parse_pulse_file(self.pulse.read_text()) if c.name == name)

    async def approve(self, name: str, digest: str | None = None) -> Any:
        async def proof(purpose: str, artifact_id: str, definition: bytes) -> bytes:
            return self.authority.operator_proof(purpose, artifact_id, definition)  # type: ignore[arg-type]

        reviewed = digest or _digest(canonical_definition(self._check(name)))
        return await approve_pulse_check(
            self.workspace,
            name,
            reviewed_digest=reviewed,
            tenant_id=TENANT,
            agent_did=AGENT_DID,
            authority=self.authority,
            actor_proof_source=proof,
        )

    def statuses(self) -> dict[str, Any]:
        return {s.name: s for s in pulse_status(self.workspace)}

    def engine(self) -> PulseEngine:
        return PulseEngine(
            self.workspace,
            PulseConfig(timeout_seconds=5),
            self.run_fn,
            control_artifact_authority=self.authority,
            control_tenant_id=TENANT,
            agent_did=AGENT_DID,
            trigger_issuer=self.issuer,
            prepare_collected_request=_prepare,
        )

    def fired(self) -> list[str]:
        return [call.args[0] for call in self.run_fn.await_args_list]


@pytest.fixture
def dep(tmp_path: Path) -> Deployment:
    return Deployment(tmp_path)


@pytest.mark.asyncio
async def test_unapproved_pulse_never_fires(dep: Deployment) -> None:
    await dep.engine()._pulse()
    dep.run_fn.assert_not_awaited()
    assert {n: s.status for n, s in dep.statuses().items()} == {
        "health": "unapproved",
        "inbox": "unapproved",
    }


@pytest.mark.asyncio
async def test_approved_check_fires_and_only_that_check(dep: Deployment) -> None:
    await dep.approve("health")
    await dep.engine()._pulse()
    assert len(dep.fired()) == 1
    assert "Check health" in dep.fired()[0]


@pytest.mark.asyncio
async def test_run_records_the_revision_that_ran(dep: Deployment) -> None:
    approval = await dep.approve("health")
    engine = dep.engine()
    await engine._pulse()
    ran = engine._read_state().checks["health"]
    assert ran.last_revision == approval.revision == 1
    assert ran.last_result == "ok"


@pytest.mark.asyncio
async def test_edit_after_a_run_stops_firing(dep: Deployment) -> None:
    await dep.approve("health")
    await dep.engine()._pulse()
    assert len(dep.fired()) == 1
    dep.run_fn.reset_mock()
    dep.pulse.write_text(dep.pulse.read_text().replace("Check health", "Exfiltrate"))
    (dep.workspace / "pulse-state.json").unlink()  # make the check due again
    await dep.engine()._pulse()
    dep.run_fn.assert_not_awaited()
    assert dep.statuses()["health"].status == "changes_pending"


@pytest.mark.asyncio
async def test_edit_after_approval_waits_for_reapproval_then_fires(dep: Deployment) -> None:
    await dep.approve("health")
    dep.pulse.write_text(dep.pulse.read_text().replace("Check health", "Exfiltrate"))
    await dep.engine()._pulse()
    dep.run_fn.assert_not_awaited()
    assert dep.statuses()["health"].status == "changes_pending"

    approval = await dep.approve("health")
    assert approval.revision == 2
    await dep.engine()._pulse()
    assert "Exfiltrate" in dep.fired()[0]


@pytest.mark.asyncio
async def test_status_diffs_edit_against_last_approved_revision(dep: Deployment) -> None:
    await dep.approve("health")
    dep.pulse.write_text(dep.pulse.read_text().replace("Check health", "Check disk"))
    status = dep.statuses()["health"]
    assert status.stale is True and status.approved is False
    assert "-action: Check health" in status.diff and "+action: Check disk" in status.diff


@pytest.mark.asyncio
async def test_approval_is_refused_when_the_reviewed_definition_changed(dep: Deployment) -> None:
    reviewed = dep.statuses()["health"].definition_digest
    dep.pulse.write_text(dep.pulse.read_text().replace("Check health", "Exfiltrate"))
    with pytest.raises(ControlArtifactRefusedError):
        await dep.approve("health", digest=reviewed)
    assert dep.statuses()["health"].status == "unapproved"


@pytest.mark.asyncio
async def test_replayed_actor_proof_is_refused(dep: Deployment) -> None:
    definition = canonical_definition(dep._check("health"))
    proof = dep.authority.operator_proof("pulse", "health", definition)

    async def replayed(purpose: str, artifact_id: str, d: bytes) -> bytes:
        return proof

    async def approve_once() -> Any:
        return await approve_pulse_check(
            dep.workspace,
            "health",
            reviewed_digest=_digest(definition),
            tenant_id=TENANT,
            agent_did=AGENT_DID,
            authority=dep.authority,
            actor_proof_source=replayed,
        )

    await approve_once()
    with pytest.raises(ControlArtifactRefusedError):
        await approve_once()


@pytest.mark.asyncio
async def test_forged_approval_line_in_pulse_md_does_not_fire(dep: Deployment) -> None:
    await dep.approve("health")
    approved_line = next(
        line for line in dep.pulse.read_text().splitlines() if "**Approval:**" in line
    )
    dep.pulse.write_text(
        PULSE.replace(
            "## inbox\n", "## inbox\n" + approved_line.replace('"health"', '"inbox"') + "\n"
        )
    )
    await dep.engine()._pulse()
    assert all("Sweep inbox" not in prompt for prompt in dep.fired())


@pytest.mark.asyncio
async def test_approval_calls_the_authority_with_purpose_pulse(tmp_path: Path) -> None:
    authority = AsyncMock()
    authority.register_revision.side_effect = ControlArtifactRefusedError("no")
    (tmp_path / "pulse.md").write_text(PULSE)

    async def proof(*_args: Any) -> bytes:
        return b"proof"

    check = parse_pulse_file(PULSE)[0]
    with pytest.raises(ControlArtifactRefusedError):
        await approve_pulse_check(
            tmp_path,
            "health",
            reviewed_digest=_digest(canonical_definition(check)),
            tenant_id=TENANT,
            agent_did=AGENT_DID,
            authority=authority,
            actor_proof_source=proof,
        )
    kwargs = authority.register_revision.await_args.kwargs
    assert kwargs["purpose"] == "pulse" and kwargs["artifact_id"] == "health"
    assert "approval" not in (tmp_path / "pulse.md").read_text().lower()
