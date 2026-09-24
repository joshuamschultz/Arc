"""Real memory and fleet stores refuse unverified nightly promotion (SPEC-083)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from arcmemory import distill
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig as ArcMemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.distill import InsightCandidate
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.insight import InsightStore
from arcmemory.types import Event, Scope
from arcteam.shared_knowledge import FleetSharedKnowledgeService
from arctrust import AgentIdentity

from arcagent.knowledge import KnowledgeAccess, KnowledgeRef, PromotionSource
from arcagent.modules.memory import _runtime
from arcagent.modules.memory._runtime import _State
from arcagent.modules.memory.capabilities import consolidate_poll_once
from arcagent.modules.memory.config import MemoryConfig

_COMPANY = "The month-end close runs on the third business day."
_PII = "Reach the vendor rep at alex.rivera@example.com for the close schedule."


class _ScoredInsightCandidate(InsightCandidate):
    """A mint candidate carrying the distiller's rubric score (mirrors T-1121)."""

    personal_score: int | None = None


class _FakeMint:
    """Structural ``InsightMint`` stand-in that preserves the extra score field."""

    def __init__(self, insights: list[_ScoredInsightCandidate]) -> None:
        self.insights = insights


class _ScoringDistiller:
    """Fake distiller (a fake MODEL): its mint payload carries fixed rubric scores."""

    def __init__(self, insights: list[_ScoredInsightCandidate]) -> None:
        self._insights = insights

    async def mint_insights(self, events: list[Event], facts: list[object]) -> _FakeMint:
        return _FakeMint(self._insights)


class _PortDraft:
    """A neutral draft the personal adapter can persist for promotion."""

    def __init__(self, source: PromotionSource) -> None:
        self.title = source.title
        self.content = source.content
        self.classification = "UNCLASSIFIED"
        self.tags = source.tags
        self.document_type = source.document_type


class _RealFleetSharedPort:
    """A REAL ``SharedKnowledgePort`` over the signed ``FleetSharedKnowledge`` store.

    ``promote`` persists the source as A's owned personal export and pushes it
    through the fleet's signed, audited ``promote`` path — no mock. Search/read/
    revoke delegate to the same live service. The test injects this port to prove
    that port possession alone cannot authorize automatic sharing.
    """

    def __init__(
        self,
        service: FleetSharedKnowledgeService,
        *,
        signer: AgentIdentity,
        personal: PersonalKnowledgeAdapter,
    ) -> None:
        self._service = service
        self._signer = signer
        self._personal = personal

    async def save(self, draft: object, access: KnowledgeAccess) -> KnowledgeRef:  # pragma: no cover
        raise NotImplementedError

    async def read(self, reference: str, access: KnowledgeAccess) -> object:
        return await self._service.read(reference, access)

    async def search(self, query: str, access: KnowledgeAccess) -> list[object]:
        return await self._service.search(query, access)

    async def promote(self, source: PromotionSource, access: KnowledgeAccess) -> KnowledgeRef:
        ref = await self._personal.save(_PortDraft(source), access)
        shared = await self._service.promote(self._personal, ref.identifier, access, self._signer)
        return KnowledgeRef(scope="shared", identifier=shared.identifier, digest=shared.digest)

    async def revoke(self, reference: str, access: KnowledgeAccess) -> None:
        await self._service.revoke(reference, access)


async def _seed_scored_insights(workspace: Path, agent_did: str) -> None:
    """Mint a clean company insight and a PII insight into A's REAL private store.

    Both are minted through the real ``distill.mint_insights`` path with a fake
    MODEL, so each carries its rubric ``personal_score`` — the company insight is a
    genuine low score (2), the PII insight is ALSO low (2) so only the raise-only
    filter can stop it.
    """
    mem_config = ArcMemoryConfig()
    db = MemoryDB(workspace)
    graph = WeightedGraph(db, mem_config)
    store = InsightStore(workspace)
    scope = Scope(agent_did=agent_did)
    distiller = _ScoringDistiller(
        [
            _ScoredInsightCandidate(
                id="company-close",
                statement=_COMPANY,
                trigger="a close-timing question",
                personal_score=2,
            ),
            _ScoredInsightCandidate(
                id="pii-contact",
                statement=_PII,
                trigger="a vendor-contact question",
                personal_score=2,
            ),
        ]
    )
    await distill.mint_insights(
        [Event(event_id="e1", scope=scope.key, kind="respond", text="close timing")],
        [],
        distiller=distiller,
        store=store,
        graph=graph,
        scope=scope,
        config=mem_config,
    )


class _Access:
    def __init__(self, identity: AgentIdentity, clearance: str = "UNCLASSIFIED") -> None:
        self.caller_did = identity.did
        self.clearance = clearance


async def test_unverified_scored_cards_cannot_reach_other_agent(tmp_path: Path) -> None:
    """A fleet port alone cannot authorize sharing from mutable memory cards."""
    _runtime.reset()
    try:
        agent_a = AgentIdentity.generate("test", "agent-a")
        agent_b = AgentIdentity.generate("test", "agent-b")
        service = FleetSharedKnowledgeService.for_arc_team(tmp_path)

        workspace = tmp_path / "agent-a" / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        await _seed_scored_insights(workspace, agent_a.did)

        # Sanity: both scored insights are really in A's private store.
        seeded = InsightStore(workspace)
        assert seeded.read("company-close") is not None
        assert seeded.read("pii-contact") is not None

        personal = PersonalKnowledgeAdapter(tmp_path / "agent-a" / "personal", agent_a.did)
        port = _RealFleetSharedPort(service, signer=agent_a, personal=personal)
        brain = ArcMemoryBrain(
            workspace,
            agent_a.did,
            distiller=_ScoringDistiller([]),
        )
        state = _State(
            config=MemoryConfig(promotion={"enabled": True}),
            brain=brain,
            workspace=workspace,
            telemetry=None,
            bus=None,
            agent_did=agent_a.did,
            active=True,
            knowledge_access=KnowledgeAccess(agent_a.did, "UNCLASSIFIED"),
            shared_knowledge=port,
        )
        _runtime.bind(state)

        # A raw scored card has no trusted score grant, even with a shared port injected.
        window = datetime.now().astimezone().replace(hour=4, minute=30, second=0, microsecond=0)
        from arcagent.modules.memory.promotion import MemoryPromotionUnavailableError

        with pytest.raises(MemoryPromotionUnavailableError):
            await consolidate_poll_once(now_local=window)

        b_access = _Access(agent_b)
        assert await service.search("month-end close", b_access) == []
        assert await service.search("alex.rivera@example.com", b_access) == []
        assert await service.list_documents(b_access) == []

        from arcagent.modules.memory.promotion import to_promotion_source

        original = seeded.read("company-close")
        assert original is not None
        access = KnowledgeAccess(agent_a.did, "UNCLASSIFIED")
        shared = await port.promote(to_promotion_source(original), access)
        await service.revoke(shared.identifier, access)
        seeded.write(original.model_copy(update={"statement": "Changed close schedule is Friday."}))

        with pytest.raises(MemoryPromotionUnavailableError):
            await consolidate_poll_once(now_local=window)
        assert await service.search("Changed close schedule", b_access) == []
    finally:
        _runtime.reset()


async def test_configured_memory_without_trusted_fleet_composition_is_unavailable(
    tmp_path: Path,
) -> None:
    """The real configure seam never silently skips an enabled promotion feature."""
    from arcagent.modules.memory.promotion import MemoryPromotionUnavailableError

    identity = AgentIdentity.generate("test", "standalone")
    _runtime.reset()
    try:
        _runtime.configure(
            config={"brain": "arcmemory", "promotion": {"enabled": True}},
            workspace=tmp_path, agent_did=identity.did, identity=identity,
        )
        window = datetime.now().astimezone().replace(hour=4, minute=59, second=0)
        with pytest.raises(MemoryPromotionUnavailableError):
            await consolidate_poll_once(now_local=window)
    finally:
        _runtime.reset()
