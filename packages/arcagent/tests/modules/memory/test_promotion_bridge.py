"""T-1203 (SPEC-083 COMP-022 / COMP-023) — ``SharedKnowledgePublisher``.

RED intent: ``arcagent.modules.memory.promotion`` still holds the retired
Phase-1 bridge (``promote_item`` / ``to_promotion_source``); it has no
``SharedKnowledgePublisher``, so every publisher test fails with ``ImportError``.
(``arcmemory.promotion.publisher`` is written in parallel by the T-1198 builder.)

Contract assumed (SDD COMP-022/023; names the SDD does not fix are marked *):

- ``SharedKnowledgePublisher(port=, exporter=, access_factory=)`` implements
  arcmemory's ``PromotionPublisher``:
  ``async publish(reference, *, content_sha256, confidence, classifier_version) -> str``.
- ``access_factory() -> KnowledgeAccess`` is the runtime identity. The publisher
  uses exactly that access for the export AND the shared promote — the origin DID
  never comes from item content.
- It re-exports ``reference`` through ``exporter.export_for_promotion`` and refuses
  (``PublisherUnavailableError``, nothing sent to the port) when the source's
  digest is not ``content_sha256`` or the source names another reference.
- *It calls ``port.promote(source, access, decision="classifier_promote",
  confidence=..., classifier_version=...)`` — the arcagent ``SharedKnowledgePort``
  gains those keyword-only pass-throughs, mirroring
  ``FleetSharedKnowledgeService.promote``.
- It returns the shared reference's ``identifier``.

Error mapping (read from ``arcteam/shared_knowledge/service.py`` +
``backend.py``; the backend authorizes and verifies before ``_atomic_write_text``):

- pre-write → ``PublisherUnavailableError``: exporter ``PermissionError`` /
  ``LookupError`` / ``ValueError``; port ``PermissionError`` (owner != caller,
  clearance, signer-does-not-match-DID, TOFU key changed, type not promotable).
- uncertain → ``PublishOutcomeUnknownError``: any other port failure (e.g. an
  ``OSError`` from the write), and a returned reference that is not a ``shared``
  reference to exactly ``content_sha256`` (something was written, not what we sent).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcmemory.adapters.memory_export import ConsolidatedMemoryExporter
from arcmemory.promotion.publisher import PublisherUnavailableError, PublishOutcomeUnknownError
from arcmemory.promotion.render import render_candidate
from arcmemory.stores.insight import InsightStore
from arcmemory.types import Insight
from arctrust.audit import AuditEvent
from arctrust.identity import did_from_public_key
from arctrust.signer import InProcessSigner

from arcagent.brain import NullBrain
from arcagent.knowledge import KnowledgeAccess, KnowledgeRef
from arcagent.modules.memory._runtime import _State
from arcagent.modules.memory.config import MemoryConfig

_SIGNER_A = InProcessSigner(b"\x0a" * 32)
_SIGNER_B = InProcessSigner(b"\x0b" * 32)
_DID_A = did_from_public_key(_SIGNER_A.public_key, org="test", agent_type="agent")
_DID_B = did_from_public_key(_SIGNER_B.public_key, org="test", agent_type="agent")
_CLEARANCE = "unclassified"
_INSIGHT = Insight(
    id="acme-renewal",
    statement="Acme renewal closes at $42k/yr; procurement needs a signed PO.",
    trigger="an Acme renewal question",
)
_REF = "insight:acme-renewal"
_DIGEST = render_candidate(_INSIGHT).content_sha256


class _FakeSharedPort:
    """A structural ``SharedKnowledgePort``: records promotes, answers as scripted."""

    def __init__(self, *, exc: BaseException | None = None, ref: KnowledgeRef | None = None):
        self._exc = exc
        self._ref = ref
        self.promoted: list[tuple[Any, KnowledgeAccess, dict[str, Any]]] = []

    async def save(self, draft: Any, access: KnowledgeAccess) -> KnowledgeRef:  # pragma: no cover
        raise NotImplementedError

    async def read(self, reference: str, access: KnowledgeAccess) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def search(self, query: str, access: KnowledgeAccess) -> list[Any]:  # pragma: no cover
        return []

    async def promote(self, source: Any, access: KnowledgeAccess, **kwargs: Any) -> KnowledgeRef:
        self.promoted.append((source, access, kwargs))
        if self._exc is not None:
            raise self._exc
        if self._ref is not None:
            return self._ref
        return KnowledgeRef(scope="shared", identifier="shared-7", digest=source.digest)

    async def revoke(self, reference: str, access: KnowledgeAccess) -> None:  # pragma: no cover
        return None


def _stores(workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(insights=InsightStore(workspace), procedures=None, entities=None)


def _workspace(tmp_path: Path, insight: Insight = _INSIGHT) -> Path:
    InsightStore(tmp_path).write(insight)
    return tmp_path


def _publisher(
    workspace: Path,
    port: Any,
    *,
    exporter_did: str = _DID_A,
    access: KnowledgeAccess | None = None,
    exporter: Any = None,
) -> Any:
    from arcagent.modules.memory.promotion import SharedKnowledgePublisher

    runtime_access = access or KnowledgeAccess(caller_did=_DID_A, clearance=_CLEARANCE)
    return SharedKnowledgePublisher(
        port=port,
        exporter=exporter
        or ConsolidatedMemoryExporter(workspace, exporter_did, _stores(workspace)),
        access_factory=lambda: runtime_access,
    )


async def _publish(publisher: Any, reference: str = _REF, digest: str = _DIGEST) -> str:
    result: str = await publisher.publish(
        reference, content_sha256=digest, confidence=0.97, classifier_version="jev-1.13.0"
    )
    return result


# -- happy path ----------------------------------------------------------------


async def test_publish_promotes_verified_source_with_classifier_decision(tmp_path: Path) -> None:
    port = _FakeSharedPort()

    shared_ref = await _publish(_publisher(_workspace(tmp_path), port))

    assert shared_ref == "shared-7"
    (source, _access, kwargs) = port.promoted[0]
    assert len(port.promoted) == 1
    assert kwargs == {
        "decision": "classifier_promote",
        "confidence": 0.97,
        "classifier_version": "jev-1.13.0",
    }
    assert source.digest == _DIGEST
    assert source.reference.scope == "personal"
    assert source.reference.identifier == _REF
    assert _INSIGHT.statement in source.content


async def test_publish_uses_the_runtime_identity_access_for_the_promote(tmp_path: Path) -> None:
    runtime_access = KnowledgeAccess(caller_did=_DID_A, clearance=_CLEARANCE)
    port = _FakeSharedPort()

    await _publish(_publisher(_workspace(tmp_path), port, access=runtime_access))

    (_source, used_access, _kwargs) = port.promoted[0]
    assert used_access is runtime_access
    assert used_access.caller_did == _DID_A


# -- pre-write refusals: nothing reaches the shared port -----------------------


async def test_changed_bytes_since_classification_are_refused_before_the_port(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    # The card changed after the classifier saw it: the old digest no longer matches.
    InsightStore(workspace).write(
        _INSIGHT.model_copy(update={"statement": "Acme; also my kid's recital is Friday."})
    )
    port = _FakeSharedPort()

    with pytest.raises(PublisherUnavailableError):
        await _publish(_publisher(workspace, port))

    assert port.promoted == []


async def test_source_owned_by_another_did_is_refused_before_the_port(tmp_path: Path) -> None:
    """Runtime identity is A; the exporter's memory belongs to B (confused deputy)."""
    port = _FakeSharedPort()

    with pytest.raises(PublisherUnavailableError):
        await _publish(_publisher(_workspace(tmp_path), port, exporter_did=_DID_B))

    assert port.promoted == []


async def test_exporter_answering_for_a_different_reference_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    real = ConsolidatedMemoryExporter(workspace, _DID_A, _stores(workspace))

    class _SwappingExporter:
        async def export_for_promotion(self, reference: str, access: Any) -> Any:
            source = await real.export_for_promotion(reference, access)
            return replace(source, reference=replace(source.reference, identifier="insight:other"))

    port = _FakeSharedPort()

    with pytest.raises(PublisherUnavailableError):
        await _publish(_publisher(workspace, port, exporter=_SwappingExporter()))

    assert port.promoted == []


async def test_missing_card_is_a_pre_write_refusal(tmp_path: Path) -> None:
    port = _FakeSharedPort()

    with pytest.raises(PublisherUnavailableError):
        await _publish(_publisher(tmp_path, port), reference="insight:gone")

    assert port.promoted == []


async def test_shared_side_permission_refusal_is_pre_write(tmp_path: Path) -> None:
    port = _FakeSharedPort(exc=PermissionError("shared knowledge signer does not match owner DID"))

    with pytest.raises(PublisherUnavailableError):
        await _publish(_publisher(_workspace(tmp_path), port))


# -- uncertain outcomes --------------------------------------------------------


async def test_write_failure_on_the_shared_side_is_outcome_unknown(tmp_path: Path) -> None:
    port = _FakeSharedPort(exc=OSError("disk full during atomic rename"))

    with pytest.raises(PublishOutcomeUnknownError):
        await _publish(_publisher(_workspace(tmp_path), port))


async def test_returned_reference_to_other_bytes_is_outcome_unknown(tmp_path: Path) -> None:
    port = _FakeSharedPort(ref=KnowledgeRef(scope="shared", identifier="s-1", digest="sha256:00"))

    with pytest.raises(PublishOutcomeUnknownError):
        await _publish(_publisher(_workspace(tmp_path), port))


async def test_returned_non_shared_reference_is_outcome_unknown(tmp_path: Path) -> None:
    port = _FakeSharedPort(ref=KnowledgeRef(scope="personal", identifier="p-1", digest=_DIGEST))

    with pytest.raises(PublishOutcomeUnknownError):
        await _publish(_publisher(_workspace(tmp_path), port))


# -- against the real fleet shared-knowledge service ---------------------------


class _DurableSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.durable: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self.durable.append(event)
        self.events.append(event)


class _OneSource:
    def __init__(self, source: Any) -> None:
        self._source = source

    async def export_for_promotion(self, reference: str, access: Any) -> Any:
        return self._source


class _ServicePort(_FakeSharedPort):
    """Test glue: the arcteam service behind the arcagent port shape (signer bound)."""

    def __init__(self, service: Any, signer: Any, sink: _DurableSink) -> None:
        super().__init__()
        self._service, self._signer, self._sink = service, signer, sink

    async def promote(self, source: Any, access: KnowledgeAccess, **kwargs: Any) -> KnowledgeRef:
        self.promoted.append((source, access, kwargs))
        result = await self._service.promote(
            _OneSource(source),
            source.reference.identifier,
            access,
            self._signer,
            audit_sink=self._sink,
            **kwargs,
        )
        return KnowledgeRef(scope=result.scope, identifier=result.identifier, digest=result.digest)


def _service(tmp_path: Path) -> Any:
    from arcteam.shared_knowledge import FleetSharedKnowledgeService

    return FleetSharedKnowledgeService.for_team_root(tmp_path / "team")


async def test_real_service_publishes_attributed_to_the_runtime_did(tmp_path: Path) -> None:
    service, sink = _service(tmp_path), _DurableSink()
    access = KnowledgeAccess(caller_did=_DID_A, clearance=_CLEARANCE)
    workspace = _workspace(tmp_path / "agent-a")

    shared_ref = await _publish(
        _publisher(workspace, _ServicePort(service, _SIGNER_A, sink), access=access)
    )

    (summary,) = await service.list_documents(access)
    assert summary.reference.identifier == shared_ref
    assert summary.owner_did == _DID_A
    decisions = [e for e in sink.durable if e.action == "knowledge.promotion_decision"]
    assert len(decisions) == 1
    assert decisions[0].actor_did == _DID_A
    assert decisions[0].extra["decision"] == "classifier_promote"
    assert decisions[0].extra["classifier_version"] == "jev-1.13.0"


async def test_real_service_refuses_a_key_not_matching_the_caller_did(tmp_path: Path) -> None:
    """Runtime DID is A but the bound signer is B's key: refused, audited deny,
    nothing written, and surfaced to the sweep as a pre-write refusal."""
    service, sink = _service(tmp_path), _DurableSink()
    access = KnowledgeAccess(caller_did=_DID_A, clearance=_CLEARANCE)
    workspace = _workspace(tmp_path / "agent-a")

    with pytest.raises(PublisherUnavailableError):
        await _publish(
            _publisher(workspace, _ServicePort(service, _SIGNER_B, sink), access=access)
        )

    denied = [
        e for e in sink.events if e.action == "knowledge.collection_saved" and e.outcome == "deny"
    ]
    assert len(denied) == 1
    assert await service.list_documents(access) == []


class _NonDurableSink:
    def write(self, event: AuditEvent) -> None:
        return None


async def test_real_service_refusing_an_unauditable_decision_is_pre_write(
    tmp_path: Path,
) -> None:
    """The service refuses a classifier promotion it cannot audit durably. That is a
    refusal before any write, so the sweep keeps the row pending — never outcome-unknown."""
    service = _service(tmp_path)
    access = KnowledgeAccess(caller_did=_DID_A, clearance=_CLEARANCE)
    workspace = _workspace(tmp_path / "agent-a")
    port = _ServicePort(service, _SIGNER_A, _NonDurableSink())  # type: ignore[arg-type]  # reason: the refusal under test

    with pytest.raises(PublisherUnavailableError):
        await _publish(_publisher(workspace, port, access=access))

    assert await service.list_documents(access) == []


async def test_real_service_write_failure_is_outcome_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    access = KnowledgeAccess(caller_did=_DID_A, clearance=_CLEARANCE)
    workspace = _workspace(tmp_path / "agent-a")

    async def dropped(*_: Any) -> Any:
        raise ConnectionError("connection dropped after send")

    monkeypatch.setattr(service.backend, "save", dropped)

    with pytest.raises(PublishOutcomeUnknownError):
        await _publish(
            _publisher(workspace, _ServicePort(service, _SIGNER_A, _DurableSink()), access=access)
        )


# -- runtime state -------------------------------------------------------------


def test_state_can_hold_a_shared_port(tmp_path: Path) -> None:
    port = _FakeSharedPort()

    state = _State(
        config=MemoryConfig(),
        brain=NullBrain(),
        workspace=tmp_path,
        telemetry=None,
        bus=None,
        agent_did=_DID_A,
        active=False,
        shared_knowledge=port,
    )

    assert state.shared_knowledge is port
