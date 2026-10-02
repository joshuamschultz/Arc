"""`arc agent run/serve/chat` build the skill revision anchor WITH an audit sink.

The anchor factory exists before the agent starts, while the agent's audit chain
exists only after ``startup``. The CLI passes a forwarder bound to the agent it
loads, so every revision-anchor event (the local-custody warning, each advance)
lands in that agent's audit trail instead of being dropped.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import arcagent
import pytest
from arctrust import AuditEvent

from arccli.commands import _serve
from arccli.commands.agent import _common, chat, run, serve


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))


def _event() -> AuditEvent:
    return AuditEvent(actor_did="did:arc:operator:x", action="a", target="t", outcome="ok")


def test_forwarder_drops_until_the_agent_has_a_chain_then_forwards() -> None:
    forwarder = _serve.AgentAuditForwarder()
    forwarder.write(_event())  # no agent bound yet: dropped, never raises

    class _NotStarted:
        @property
        def audit_sink(self) -> Any:
            raise arcagent.ExtensionError(code="AGENT_NOT_STARTED", message="not started")

    forwarder.bind(_NotStarted())
    forwarder.write(_event())  # agent not started: dropped, never raises

    events: list[AuditEvent] = []
    forwarder.bind(SimpleNamespace(audit_sink=SimpleNamespace(write=events.append)))
    forwarder.write(_event())
    assert [e.action for e in events] == ["a"]


def test_cli_agent_loader_wires_an_audit_bound_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_dir = tmp_path / "ada"
    agent_dir.mkdir()
    (agent_dir / "arcagent.toml").write_text('[agent]\nname = "ada"\n', encoding="utf-8")
    captured: dict[str, Any] = {}

    def _factory(audit_sink: Any = None) -> Any:
        captured["sink"] = audit_sink
        return None

    monkeypatch.setattr(_common, "build_skill_revision_anchor_factory", _factory)
    agent, _config, _path = _common.load_cli_agent(agent_dir)
    sink = captured["sink"]
    assert isinstance(sink, _serve.AgentAuditForwarder)
    assert sink.agent is agent


@pytest.mark.parametrize("module", [run, serve, chat])
def test_run_serve_and_chat_load_through_the_audited_loader(module: Any) -> None:
    assert module.load_cli_agent is _common.load_cli_agent
    assert not hasattr(module, "build_skill_revision_anchor_factory")
