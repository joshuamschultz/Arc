"""The workflow_run tool labels its runs as agent-initiated.

An unsigned draft may run only for an authenticated operator, so a regression
that relabels this tool as "operator" would let an agent run its own drafts.
"""

from __future__ import annotations

from typing import Any

import pytest
from packages.arcagent.tests.unit.modules.workflows.conftest import (
    FakeResult,
    RecordingControlPlane,
)

pytestmark = pytest.mark.usefixtures("workflows_state")


@pytest.mark.asyncio
async def test_workflow_run_tool_passes_initiator_agent(
    workflows_state: RecordingControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcagent.modules.workflows import capabilities
    from arcagent.modules.workflows.workflow_capabilities import tools

    seen: list[dict[str, Any]] = []

    async def spy_run(workflow_id: str, **kwargs: Any) -> FakeResult:
        seen.append(kwargs)
        return FakeResult(ok=False)

    async def no_activation_gate(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(workflows_state, "run", spy_run, raising=False)
    monkeypatch.setattr(tools, "_activation_refusal", no_activation_gate)
    await capabilities.workflow_create(
        workflow_id="wf", nodes=[{"id": "collect", "kind": "agent", "agent": "@sales"}]
    )

    await capabilities.workflow_run(workflow_id="wf")

    assert [call["initiator"] for call in seen] == ["agent"]
