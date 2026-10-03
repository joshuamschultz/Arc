"""Journey: a started agent's trifecta gate honours the operator's "Always allow".

The 2026-10-03 incident (103 Approve clicks for one chat) is replayed end to end
in ``packages/arcagent/tests/security/test_trifecta_repeat_prompts.py`` over the
real registry, gate, channel and store. This journey proves the production
wiring those parts arrive in: the agent ``arc agent run`` builds asks the
operator through the arcstore approval channel AND reads the operator's standing
grants from that same channel — so a grant written by arcui's "Always allow" or
``arc approve --always`` is the one the gate consults. A gate built without its
standing source would pass every unit test and still prompt forever.
"""

from __future__ import annotations

from .conftest import Deployment


async def test_started_agent_reads_standing_grants_from_its_approval_channel(
    deployment: Deployment,
) -> None:
    from arcagent.tools.approval_channel import ArcStoreApprovalChannel
    from arcagent.tools.human_gate import HumanGate
    from arccli.commands.agent._common import load_cli_agent

    agent, _config, _path = load_cli_agent(deployment.agent_dir)
    await agent.startup()
    try:
        gate = agent._human_gate
        assert isinstance(gate, HumanGate)
        assert isinstance(gate._channel, ArcStoreApprovalChannel)
        assert gate._standing is gate._channel
        assert agent._tool_registry is not None
        assert agent._tool_registry._human_gate is gate
    finally:
        await agent.shutdown()
