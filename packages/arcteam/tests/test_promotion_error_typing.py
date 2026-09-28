"""SPEC-083 — ``FleetSharedKnowledgeService.promote`` types its failures unambiguously.

The sweep's ledger depends on telling two failures apart:

* **refused before any shared write** — :class:`SharedKnowledgePromotionRefusedError`,
  a ``PermissionError`` (the shared-knowledge seam's "refused, nothing written"
  contract) that is also a ``ValueError`` for callers that validated input. The row
  stays pending and is re-evaluated.
* **the write may have landed** — :class:`SharedKnowledgePromotionOutcomeUnknownError`.
  Never retried automatically, so nothing is shared twice.

Every failure before the shared save is a refusal (nothing was written yet); every
failure from the save onward is outcome-unknown.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity
from arctrust.audit import AuditEvent

from arcteam.shared_knowledge import (
    FleetSharedKnowledgeService,
    SharedKnowledgePromotionOutcomeUnknownError,
    SharedKnowledgePromotionRefusedError,
    SharedKnowledgeUnavailableError,
)

VERSION = "jev-1.13.0"


class _Access:
    def __init__(self, identity: AgentIdentity) -> None:
        self.caller_did = identity.did
        self.clearance = "UNCLASSIFIED"


class _Draft:
    title = "Close timing"
    content = "The month-end close runs on the third business day."
    classification = "UNCLASSIFIED"
    tags = ("insight",)
    document_type = "insight"


class _DurableSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self.events.append(event)


class _NonDurableSink:
    def write(self, event: AuditEvent) -> None:
        return None


class _FailingDurableSink(_DurableSink):
    def write_durable(self, event: AuditEvent) -> None:
        raise OSError("audit unavailable")


async def _setup(tmp_path: Path) -> tuple[Any, ...]:
    owner = AgentIdentity.generate("test", "owner")
    access = _Access(owner)
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    personal = PersonalKnowledgeAdapter(tmp_path / "owner", owner.did)
    ref = await personal.save(_Draft(), access)
    return owner, access, service, personal, ref


async def _promote(
    service: Any, personal: Any, ref: Any, access: Any, owner: Any, **kw: Any
) -> Any:
    kw.setdefault("audit_sink", _DurableSink())
    return await service.promote(
        personal,
        ref.identifier,
        access,
        owner,
        decision=kw.pop("decision", "classifier_promote"),
        confidence=kw.pop("confidence", 0.97),
        classifier_version=VERSION,
        **kw,
    )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"decision": "auto"}, "unknown promotion decision"),
        ({"confidence": 0.5}, "confidence"),
        ({"audit_sink": _NonDurableSink()}, "durable audit sink"),
        ({"audit_sink": _FailingDurableSink()}, "audit unavailable"),
    ],
    ids=["bad-decision", "low-confidence", "non-durable-sink", "failed-decision-audit"],
)
async def test_pre_write_failures_are_typed_refusals(
    tmp_path: Path, overrides: dict[str, Any], match: str
) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)

    with pytest.raises(SharedKnowledgePromotionRefusedError, match=match) as caught:
        await _promote(service, personal, ref, access, owner, **overrides)

    assert isinstance(caught.value, PermissionError)
    assert isinstance(caught.value, ValueError)
    assert await service.list_documents(access) == []


async def test_missing_collection_mechanics_is_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)

    def unavailable(*_: Any) -> Any:
        raise SharedKnowledgeUnavailableError(
            "ArcMemory shared collection mechanics are unavailable"
        )

    monkeypatch.setattr(service, "_collection", unavailable)

    with pytest.raises(SharedKnowledgePromotionRefusedError, match="unavailable"):
        await _promote(service, personal, ref, access, owner)


async def test_save_failure_is_outcome_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)

    async def dropped(*_: Any) -> Any:
        raise ConnectionError("connection dropped after send")

    monkeypatch.setattr(service.backend, "save", dropped)

    with pytest.raises(SharedKnowledgePromotionOutcomeUnknownError) as caught:
        await _promote(service, personal, ref, access, owner)

    assert not isinstance(caught.value, PermissionError)


async def test_invalid_reference_after_write_is_outcome_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)
    real_save = service.backend.save

    async def wrong_digest(draft: Any, caller: Any) -> Any:
        result = await real_save(draft, caller)
        return type(result)(result.scope, result.identifier, "sha256:" + "0" * 64)

    monkeypatch.setattr(service.backend, "save", wrong_digest)

    with pytest.raises(SharedKnowledgePromotionOutcomeUnknownError, match="invalid reference"):
        await _promote(service, personal, ref, access, owner)


async def test_port_refuses_an_unparseable_clearance_as_a_refusal(tmp_path: Path) -> None:
    """A caller clearance label the port cannot parse is refused before any write
    as ``PermissionError`` — the seam's refusal contract — not a bare ``ValueError``."""
    from arcteam.shared_knowledge import FleetSharedKnowledgePort

    owner, access, service, personal, ref = await _setup(tmp_path)
    port = FleetSharedKnowledgePort(
        service, access=access, signer=owner, audit_sink=_DurableSink()
    )
    source = await personal.export_for_promotion(ref.identifier, access)
    forged = _Access(owner)
    forged.clearance = "NOT-A-LEVEL"

    with pytest.raises(PermissionError):
        await port.promote(
            source,
            forged,
            decision="classifier_promote",
            confidence=0.97,
            classifier_version=VERSION,
        )
