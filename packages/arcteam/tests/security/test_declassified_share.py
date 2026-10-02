"""Alpha-2 item 16 follow-up — the declassified-at-source share.

A CUI-cleared agent may share an unclassified card AS unclassified. That is the ONE
exception to the shared store's no-write-down rule, and it holds only when:

* the label is the card's own stored label, attested by the consolidated-memory
  exporter (``label_from_card``) — never a label the caller names;
* a classifier or operator decision is being recorded for the write; and
* the label is strictly below the writer's clearance.

Everything else stays strict: a label above the clearance, an unattested source
(the generic personal adapter), an undecided write, and a direct backend save.
Real objects: the service, backend, exporter, stores, identities. Faked: the sink.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from arcmemory.adapters.memory_export import ConsolidatedMemoryExporter
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arcmemory.adapters.shared_knowledge import SharedKnowledgeAdapter
from arcmemory.stores.insight import InsightStore
from arcmemory.types import Insight
from arctrust import AgentIdentity
from arctrust.audit import AuditEvent

from arcteam.shared_knowledge import FleetSharedKnowledgeService

_VERSION = "jev-1.13.0"
_REF = "insight:acme-renewal"


class _Access:
    def __init__(self, identity: AgentIdentity, clearance: str) -> None:
        self.caller_did = identity.did
        self.clearance = clearance


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self.events.append(event)

    def decisions(self) -> list[AuditEvent]:
        return [e for e in self.events if e.action == "knowledge.promotion_decision"]


@pytest.fixture
def agent() -> AgentIdentity:
    return AgentIdentity.generate("test", "cui-agent")


def _card(workspace: Path, label: str) -> None:
    InsightStore(workspace).write(
        Insight(
            id="acme-renewal",
            statement="Acme renewal closes at $42k/yr.",
            trigger="a renewal",
            classification=label,
        )
    )


async def _promote(
    tmp_path: Path,
    agent: AgentIdentity,
    source: Any,
    *,
    clearance: str = "CUI",
    sink: _Sink | None = None,
    decision: str | None = "classifier_promote",
) -> tuple[Any, FleetSharedKnowledgeService]:
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path / "team")
    extra: dict[str, Any] = (
        {"confidence": 0.97, "classifier_version": _VERSION} if decision else {}
    )
    result = await service.promote(
        source,
        _REF,
        _Access(agent, clearance),
        agent,
        audit_sink=sink or _Sink(),
        decision=decision,
        **extra,
    )
    return result, service


def _exporter(tmp_path: Path, agent: AgentIdentity, label: str) -> ConsolidatedMemoryExporter:
    workspace = tmp_path / "ws"
    _card(workspace, label)
    return ConsolidatedMemoryExporter.for_workspace(workspace, agent.did)


async def test_cui_agent_shares_an_unclassified_card_as_unclassified(
    tmp_path: Path, agent: AgentIdentity
) -> None:
    sink = _Sink()
    exporter = _exporter(tmp_path, agent, "unclassified")

    ref, service = await _promote(tmp_path, agent, exporter, sink=sink)

    # Readable by an UNCLASSIFIED reader: the shared label is the honest one.
    document = await service.read(ref.identifier, _Access(agent, "UNCLASSIFIED"))
    assert document.classification == "UNCLASSIFIED"
    [decision] = sink.decisions()
    assert decision.extra["declassified_at_source"] is True
    assert (decision.extra["shared_label"], decision.extra["clearance"]) == ("UNCLASSIFIED", "CUI")
    assert decision.extra["declassified_why"]


async def test_secret_card_from_a_cui_agent_is_refused(
    tmp_path: Path, agent: AgentIdentity
) -> None:
    exporter = _exporter(tmp_path, agent, "secret")

    with pytest.raises(PermissionError):
        await _promote(tmp_path, agent, exporter)


async def test_unlabelled_card_from_a_cui_agent_is_shared_as_cui(
    tmp_path: Path, agent: AgentIdentity
) -> None:
    workspace = tmp_path / "ws"
    _card(workspace, "unclassified")
    path = InsightStore(workspace).path_for("acme-renewal")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text(
        "".join(line for line in lines if not line.startswith("classification:")),
        encoding="utf-8",
    )
    exporter = ConsolidatedMemoryExporter.for_workspace(workspace, agent.did)
    sink = _Sink()

    ref, service = await _promote(tmp_path, agent, exporter, sink=sink)

    document = await service.read(ref.identifier, _Access(agent, "CUI"))
    assert document.classification == "CUI"
    with pytest.raises(PermissionError):  # an UNCLASSIFIED reader never sees it
        await service.read(ref.identifier, _Access(agent, "UNCLASSIFIED"))
    assert "declassified_at_source" not in sink.decisions()[0].extra


async def test_a_lower_label_from_an_unattested_source_is_refused(
    tmp_path: Path, agent: AgentIdentity
) -> None:
    """The generic personal adapter never attests a label: strict no-write-down holds."""
    personal = PersonalKnowledgeAdapter(tmp_path / "personal", agent.did)

    class _Draft:
        title = "Close timing"
        content = "The month-end close runs on the third business day."
        classification = "UNCLASSIFIED"
        tags = ("insight",)
        document_type = "insight"

    ref = await personal.save(_Draft(), _Access(agent, "UNCLASSIFIED"))

    with pytest.raises(PermissionError, match="no-write-down"):
        service = FleetSharedKnowledgeService.for_arc_team(tmp_path / "team")
        await service.promote(
            personal,
            ref.identifier,
            _Access(agent, "CUI"),
            agent,
            audit_sink=_Sink(),
            decision="classifier_promote",
            confidence=0.97,
            classifier_version=_VERSION,
        )


async def test_a_forged_lower_label_above_the_card_is_refused(
    tmp_path: Path, agent: AgentIdentity
) -> None:
    """Classification laundering: a SECRET card relabelled UNCLASSIFIED by the caller."""
    exporter = _exporter(tmp_path, agent, "cui")
    honest = await exporter.export_for_promotion(_REF, _Access(agent, "CUI"))

    class _Forging:
        async def export_for_promotion(self, reference: str, access: Any) -> Any:
            # Not attested: a caller-built source, even with a lower label.
            return replace(honest, classification="UNCLASSIFIED", label_from_card=False)

    with pytest.raises(PermissionError, match="no-write-down"):
        await _promote(tmp_path, agent, _Forging())


async def test_an_undecided_write_is_never_declassified(
    tmp_path: Path, agent: AgentIdentity
) -> None:
    exporter = _exporter(tmp_path, agent, "unclassified")

    with pytest.raises(PermissionError, match="no-write-down"):
        await _promote(tmp_path, agent, exporter, decision=None)


async def test_a_direct_backend_save_below_clearance_is_still_refused(
    tmp_path: Path, agent: AgentIdentity
) -> None:
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path / "team")
    adapter = SharedKnowledgeAdapter(service.backend, owner_did=agent.did, signer=agent)

    class _Draft:
        title = "Close timing"
        content = "The month-end close runs on the third business day."
        classification = "UNCLASSIFIED"
        tags = ("insight",)
        document_type = "insight"
        declassified_at_source = False

    with pytest.raises(PermissionError, match="no-write-down"):
        await adapter.save(_Draft(), _Access(agent, "CUI"))


async def test_the_cui_agent_still_shares_a_cui_card_as_cui(
    tmp_path: Path, agent: AgentIdentity
) -> None:
    """Narrowness pair: the equal-label path is unchanged and records no declassification."""
    sink = _Sink()
    exporter = _exporter(tmp_path, agent, "cui")

    ref, service = await _promote(tmp_path, agent, exporter, sink=sink)

    assert (await service.read(ref.identifier, _Access(agent, "CUI"))).classification == "CUI"
    assert "declassified_at_source" not in sink.decisions()[0].extra
