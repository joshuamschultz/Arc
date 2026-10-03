"""Journey: pulse.md checks fire only after the operator approves their exact text.

The agent is built by the same loader ``arc agent run/serve/chat`` use, so the
control authority, trigger issuer and run path are the production ones. Before
this existed, no production code issued a pulse approval: a pulse check could be
written but never run. Now an unapproved check stays inert, the operator approval
path (``approve_pulse_check`` through the authority the agent holds) makes it
fire, and an edit afterwards stops it until it is approved again.
"""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import Deployment, ScriptedLLM
from .test_journey_modules import _SOURCE_CATALOG, install_modules

CHECK = "## briefing\n- **Interval:** 5 min\n- **Action:** Summarize the overnight alerts\n"


@pytest.fixture(autouse=True)
def _module_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(_SOURCE_CATALOG))


def _prompt_runs(llm: ScriptedLLM, prompt: str) -> int:
    return sum(
        1
        for messages in llm.calls
        if messages and prompt in str(getattr(messages[-1], "content", messages[-1]))
    )


async def test_pulse_check_fires_only_after_operator_approval(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    import arcagent
    from arcagent.modules.pulse import _runtime
    from arccli.commands.agent._common import load_cli_agent

    enable_modules("pulse")
    rows = install_modules(deployment)
    assert not [row for row in rows if "REFUSED" in row], f"install refused: {rows}"

    agent, _config, _path = load_cli_agent(deployment.agent_dir)
    await agent.startup()
    try:
        state = _runtime.state()
        engine, authority = state.engine, state.control_artifact_authority
        assert engine is not None and authority is not None, "pulse has no control authority"
        workspace = state.workspace
        pulse = workspace / "pulse.md"
        pulse.write_text(CHECK, encoding="utf-8")
        prompt = "Summarize the overnight alerts"

        await engine._pulse()
        assert _prompt_runs(scripted_llm, prompt) == 0, "an unapproved pulse check ran"
        assert [s.status for s in arcagent.pulse_status(workspace)] == ["unapproved"]

        async def operator_proof(purpose: str, artifact_id: str, definition: bytes) -> bytes:
            return authority.operator_proof(purpose, artifact_id, definition)  # type: ignore[attr-defined]

        reviewed = arcagent.pulse_status(workspace)[0].definition_digest
        approval = await arcagent.approve_pulse_check(
            workspace,
            "briefing",
            reviewed_digest=reviewed,
            tenant_id=state.control_tenant_id or "",
            agent_did=state.agent_did,
            authority=authority,
            actor_proof_source=operator_proof,
        )
        assert approval.revision == 1

        await engine._pulse()
        assert _prompt_runs(scripted_llm, prompt) == 1, "the approved pulse check did not fire"
        assert engine._read_state().checks["briefing"].last_revision == 1

        # An edit after approval is a different definition: inert until re-approved.
        pulse.write_text(pulse.read_text().replace("overnight alerts", "payroll files"))
        (workspace / "pulse-state.json").unlink()
        await engine._pulse()
        assert _prompt_runs(scripted_llm, "payroll files") == 0, "an edited pulse check ran"
        assert arcagent.pulse_status(workspace)[0].status == "changes_pending"
    finally:
        await agent.shutdown()


async def test_pulse_check_added_by_the_operator_writer_needs_approval_then_fires(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """The add/edit path (what the arcui route and ``arc pulse add`` call) never self-approves."""
    import arcagent
    from arcagent.modules.pulse import _runtime
    from arccli.commands.agent._common import load_cli_agent

    enable_modules("pulse")
    install_modules(deployment)
    agent, _config, _path = load_cli_agent(deployment.agent_dir)
    await agent.startup()
    try:
        state = _runtime.state()
        engine, authority = state.engine, state.control_artifact_authority
        assert engine is not None and authority is not None
        workspace = state.workspace
        prompt = "Watch the build queue"

        arcagent.add_pulse_check(workspace, name="queue", interval_minutes=5, action=prompt)
        await engine._pulse()
        assert _prompt_runs(scripted_llm, prompt) == 0, "a freshly added check ran unapproved"
        assert [s.status for s in arcagent.pulse_status(workspace)] == ["unapproved"]

        async def operator_proof(purpose: str, artifact_id: str, definition: bytes) -> bytes:
            return authority.operator_proof(purpose, artifact_id, definition)  # type: ignore[attr-defined]

        await arcagent.approve_pulse_check(
            workspace,
            "queue",
            reviewed_digest=arcagent.pulse_status(workspace)[0].definition_digest,
            tenant_id=state.control_tenant_id or "",
            agent_did=state.agent_did,
            authority=authority,
            actor_proof_source=operator_proof,
        )
        await engine._pulse()
        assert _prompt_runs(scripted_llm, prompt) == 1

        arcagent.edit_pulse_check(workspace, "queue", interval_minutes=5, action="Watch payroll")
        (workspace / "pulse-state.json").unlink()
        await engine._pulse()
        assert _prompt_runs(scripted_llm, "Watch payroll") == 0, "an edited check ran unapproved"
        assert arcagent.pulse_status(workspace)[0].status == "changes_pending"
    finally:
        await agent.shutdown()
