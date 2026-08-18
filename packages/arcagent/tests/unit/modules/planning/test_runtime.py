"""Planning module runtime configuration (SPEC-040)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.modules.planning import _runtime


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


class TestActorDidAttribution:
    """The audit actor DID must be the authenticated agent DID when present."""

    def test_supplied_did_wins_even_with_empty_agent_name(self, tmp_path: Path) -> None:
        # Regression: an operator-precedence bug parsed the actor_did expression
        # as ``(agent_did or f"did:arc:{name}") if name else "did:arc:planner"``,
        # so an empty agent_name discarded a real supplied DID and every plan
        # audit event was attributed to the synthetic "did:arc:planner".
        _runtime.configure(
            workspace=tmp_path,
            agent_name="",
            agent_did="did:arc:real-agent",
        )
        assert _runtime.state().store._actor_did == "did:arc:real-agent"

    def test_falls_back_to_name_did_when_no_agent_did(self, tmp_path: Path) -> None:
        _runtime.configure(workspace=tmp_path, agent_name="scout", agent_did="")
        assert _runtime.state().store._actor_did == "did:arc:scout"

    def test_falls_back_to_planner_when_nothing_supplied(self, tmp_path: Path) -> None:
        _runtime.configure(workspace=tmp_path, agent_name="", agent_did="")
        assert _runtime.state().store._actor_did == "did:arc:planner"
