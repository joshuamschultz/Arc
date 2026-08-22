"""Explicit scoped-knowledge tools composed through the registered memory module."""

from __future__ import annotations

import pytest
from arctrust import AgentIdentity

from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import (
    knowledge_promote,
    knowledge_retrieve,
    knowledge_save,
    knowledge_search,
)


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def audit_event(self, event: str, detail: dict) -> None:
        self.events.append((event, detail))


@pytest.fixture(autouse=True)
def _reset() -> None:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.mark.asyncio
async def test_registered_memory_module_exposes_explicit_personal_and_shared_tools(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "fleet"))
    identity = AgentIdentity.generate("test", "knowledge")
    _runtime.configure(
        config={"shared_knowledge_enabled": True},
        workspace=tmp_path / "agent",
        agent_did=identity.did,
        identity=identity,
    )

    saved = await knowledge_save("personal", "Runbook", "Rotate the key.")
    identifier = saved.removesuffix(".").split()[-1]
    personal = await knowledge_retrieve("personal", identifier)
    promoted = await knowledge_promote(identifier)
    shared_identifier = promoted.removesuffix(".").split()[-1]
    shared = await knowledge_retrieve("shared", shared_identifier)
    search = await knowledge_search("shared", "rotate")

    assert "Saved personal knowledge" in saved
    assert "Rotate the key." in personal
    assert "Promoted shared knowledge" in promoted
    assert "Rotate the key." in shared
    assert shared_identifier in search


@pytest.mark.asyncio
async def test_composed_shared_knowledge_promotes_through_telemetry_audit_sink(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "fleet"))
    identity = AgentIdentity.generate("test", "knowledge-audit")
    telemetry = _Telemetry()
    _runtime.configure(
        config={"shared_knowledge_enabled": True},
        telemetry=telemetry,
        workspace=tmp_path / "agent",
        agent_did=identity.did,
        identity=identity,
    )

    saved = await knowledge_save("personal", "Runbook", "Rotate the key.")
    identifier = saved.removesuffix(".").split()[-1]
    await knowledge_promote(identifier)

    assert any(event == "knowledge.promoted" for event, _ in telemetry.events)
