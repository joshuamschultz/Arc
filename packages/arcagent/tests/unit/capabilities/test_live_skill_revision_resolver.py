"""Live identity binding for the externally anchored skill resolver."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from arcagent.modules.capability_import.authority_factory import LiveSkillRevisionResolver


class _EmptyAnchor:
    def __init__(self, scope: str) -> None:
        self.scope = scope

    def latest(self) -> None:
        return None


def test_skill_authority_requires_live_did_and_pins_identity(tmp_path: Path) -> None:
    current = [""]
    calls: list[tuple[str, str]] = []

    def anchor_for(did: str, skill: str) -> _EmptyAnchor:
        calls.append((did, skill))
        return _EmptyAnchor(f"skill/{hashlib.sha256(did.encode()).hexdigest()}/{skill}")

    resolver = LiveSkillRevisionResolver(
        agent_did=lambda: current[0],
        config_path=tmp_path / "arcagent.toml",
        anchor_factory=anchor_for,
    )
    folder = tmp_path / "capabilities" / "skills" / "reporter"
    with pytest.raises(RuntimeError, match="identity is unavailable"):
        resolver.resolve(folder, "agent-skills")
    assert calls == []

    current[0] = "did:arc:agent:one"
    with pytest.raises(ValueError, match="authority is unavailable"):
        resolver.resolve(folder, "agent-skills")
    assert calls == [("did:arc:agent:one", "reporter")]

    current[0] = "did:arc:agent:two"
    with pytest.raises(RuntimeError, match="identity changed"):
        resolver.resolve(folder, "agent-skills")
    assert calls == [("did:arc:agent:one", "reporter")]


def test_skill_authority_rejects_wrong_anchor_scope(tmp_path: Path) -> None:
    resolver = LiveSkillRevisionResolver(
        agent_did=lambda: "did:arc:agent:one",
        config_path=tmp_path / "arcagent.toml",
        anchor_factory=lambda _did, _name: _EmptyAnchor("skill/other/reporter"),
    )
    with pytest.raises(ValueError, match="scope does not match"):
        resolver.resolve(tmp_path / "skills" / "reporter", "agent-skills")
