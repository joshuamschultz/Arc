"""``hermes`` — the Slice-1 reference foreign harness type (H-040 item 6).

A folder exporting ``AGENT_TYPE``, discovered by the harness-type scan. Deleting
this folder deletes the type with no core change (§11). The fleet learns nothing
about "hermes" beyond this descriptor — trust is pinned in fleet code, not here.
"""

from __future__ import annotations

from arcteam.harness.agent_type import AgentType
from arcteam.harness.protocol import HarnessAdapter
from arcteam.harness.types.hermes.adapter import HermesHarness
from arcteam.types import Entity


def _build(
    *,
    entity: Entity,
    messenger: object | None = None,
    python_executable: str | None = None,
    **_: object,
) -> HarnessAdapter:
    """Construct a live hermes member from its verified entity + fleet handles."""
    return HermesHarness(
        entity=entity,
        messenger=messenger,
        python_executable=python_executable,
    )


AGENT_TYPE = AgentType(name="hermes", build=_build)

__all__ = ["AGENT_TYPE", "HermesHarness"]
