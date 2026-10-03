"""An agent may PROPOSE a pulse check; it can never write pulse.md (S-pulse)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcagent.modules.pulse.editing import PulseCheckInvalidError
from arcagent.modules.pulse.proposals import (
    clear_proposal,
    list_proposals,
    propose_pulse_check,
)


def test_proposal_is_filed_without_touching_pulse_md(tmp_path: Path) -> None:
    propose_pulse_check(tmp_path, name="inbox", interval_minutes=30, action="Sweep", reason="r")
    assert not (tmp_path / "pulse.md").exists()
    [proposal] = list_proposals(tmp_path)
    assert proposal["name"] == "inbox"
    assert proposal["interval_minutes"] == 30
    assert proposal["reason"] == "r"


def test_proposal_is_validated_like_a_real_check(tmp_path: Path) -> None:
    with pytest.raises(PulseCheckInvalidError):
        propose_pulse_check(tmp_path, name="bad name", interval_minutes=5, action="x", reason="")


def test_proposals_are_capped(tmp_path: Path) -> None:
    for i in range(20):
        propose_pulse_check(tmp_path, name=f"c{i}", interval_minutes=5, action="x", reason="")
    with pytest.raises(PulseCheckInvalidError, match="too many"):
        propose_pulse_check(tmp_path, name="one-more", interval_minutes=5, action="x", reason="")


def test_same_name_replaces_the_pending_proposal(tmp_path: Path) -> None:
    propose_pulse_check(tmp_path, name="a", interval_minutes=5, action="one", reason="")
    propose_pulse_check(tmp_path, name="a", interval_minutes=9, action="two", reason="")
    [proposal] = list_proposals(tmp_path)
    assert proposal["action"] == "two"


def test_clear_removes_it(tmp_path: Path) -> None:
    propose_pulse_check(tmp_path, name="a", interval_minutes=5, action="x", reason="")
    clear_proposal(tmp_path, "a")
    assert list_proposals(tmp_path) == []


def test_corrupt_file_reads_as_empty(tmp_path: Path) -> None:
    (tmp_path / "pulse-proposals.json").write_text("{not json")
    assert list_proposals(tmp_path) == []
    (tmp_path / "pulse-proposals.json").write_text(json.dumps({"x": 1}))
    assert list_proposals(tmp_path) == []


def test_pulse_files_are_protected_from_agent_write_tools() -> None:
    from arcagent.tools._validation import DEFAULT_PROTECTED_NAMES

    for name in ("pulse.md", "pulse-proposals.json", "pulse-approved.json"):
        assert name in DEFAULT_PROTECTED_NAMES
