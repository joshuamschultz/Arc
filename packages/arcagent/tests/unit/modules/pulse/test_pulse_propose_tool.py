"""pulse_propose — the agent's only path toward a new pulse check (S-pulse)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arcagent.modules.pulse import _runtime, capabilities
from arcagent.modules.pulse.proposals import list_proposals


@pytest.fixture(autouse=True)
def _runtime_state(tmp_path: Path) -> None:
    _runtime.configure(workspace=tmp_path)
    yield
    _runtime.reset()


def test_tool_files_a_proposal_and_never_writes_pulse_md(tmp_path: Path) -> None:
    result = asyncio.run(
        capabilities.pulse_propose(
            name="inbox", interval_minutes=30, action="Sweep inbox", reason="mail piles up"
        )
    )
    assert "operator" in result
    assert [p["name"] for p in list_proposals(tmp_path)] == ["inbox"]
    assert not (tmp_path / "pulse.md").exists()


def test_tool_reports_invalid_input_without_raising(tmp_path: Path) -> None:
    result = asyncio.run(
        capabilities.pulse_propose(name="bad name", interval_minutes=5, action="x", reason="")
    )
    assert "not filed" in result
    assert list_proposals(tmp_path) == []
