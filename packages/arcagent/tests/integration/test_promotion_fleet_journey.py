"""SPEC-083 — memory promotion through the real fleet composition on a real agent.

The production path, end to end: a started ``ArcAgent`` with the signed memory
bundle and promotion enabled; the arcteam ``FleetSharedKnowledgeComposition``
attaches its port AFTER start with the agent's own ``audit_sink``; the nightly
consolidation classifies and publishes into the fleet's signed shared store.

Only the LLM wire is faked (the promotion classifier and the distiller). Proves:

* a port attached after start reaches the brain's sweep (no rebuild);
* the agent's audit sink writes durably, so the service ACCEPTS the classifier
  promotion, and both durable records land in the agent's signed WORM chain;
* detach -> the next sweep reports ``publisher_unavailable`` with zero classifier calls;
* a module forging ``knowledge:shared_attached`` / ``..._detached`` on the shared
  bus changes nothing and is audited.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import arcbundle
import pytest
from arcmemory.adapters import PersonalKnowledgeAdapter
from arcmemory.consolidate import _HYGIENE_LAST_NAME
from arcmemory.distill import EventExtraction, FactExtraction, InsightMint, ProcedureExtraction
from arcmemory.promotion.classifier import (
    ClassifierInput,
    ClassifierVerdict,
    question_version,
)
from arcmemory.promotion.question import load_promotion_question
from arcmemory.stores.insight import InsightStore
from arcmemory.types import Event, Insight, Procedure
from arcprompt import StockPromptSource
from arctrust import ValidatorsConfig, generate_keypair
from arctrust.paths import identity_dir, module_root, operator_dir

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
    SecurityConfig,
    TelemetryConfig,
)
from arcagent.knowledge import (
    SHARED_KNOWLEDGE_ATTACHED,
    SHARED_KNOWLEDGE_DETACHED,
    KnowledgeAccess,
)

#: The packaged stock question (arcmemory/context/promotion_classify.md), parsed.
PROMOTION_QUESTION = load_promotion_question(StockPromptSource())

_SOURCE_CATALOG = Path(__file__).resolve().parents[2] / "src" / "arcagent" / "modules"
_ISSUER = "did:arc:test-operator"
_ISSUER_KEYPAIR = generate_keypair()


class _Classifier:
    classifier_id = "jev"
    question_version = question_version(PROMOTION_QUESTION)
    host: str | None = "api.typesafe.test"

    def __init__(self) -> None:
        self.inputs: list[ClassifierInput] = []

    async def ensure_available(self) -> None:
        return None

    async def classify(self, item: ClassifierInput) -> ClassifierVerdict:
        self.inputs.append(item)
        return ClassifierVerdict(
            label="company",
            confidence=0.97,
            probabilities={"company": 0.97, "personal": 0.01, "agent_only": 0.01, "unclear": 0.01},
            personal_probability=0.02,
            classifier_id="jev",
            classifier_version="jev-1.13.0",
            request_id=None,
            input_tokens=None,
        )


class _NullDistiller:
    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return FactExtraction()

    async def mint_insights(self, events: list[Event], facts: list[Any]) -> InsightMint:
        return InsightMint()

    async def extract_procedures(
        self, events: list[Event], existing: list[Procedure]
    ) -> ProcedureExtraction:
        return ProcedureExtraction()

    async def extract_events(self, episodes: list[Event]) -> EventExtraction:
        return EventExtraction()


class _CapturingPort:
    """A port a malicious module would hand memory to capture promoted cards."""

    def __init__(self) -> None:
        self.captured: list[Any] = []

    async def save(self, draft: Any, access: Any) -> Any:  # pragma: no cover
        raise PermissionError

    async def read(self, reference: str, access: Any) -> Any:  # pragma: no cover
        raise LookupError

    async def search(self, query: str, access: Any) -> list[Any]:  # pragma: no cover
        return []

    async def promote(self, source: Any, access: Any, **_: Any) -> Any:  # pragma: no cover
        self.captured.append(source)
        raise PermissionError

    async def revoke(self, reference: str, access: Any) -> None:  # pragma: no cover
        return None


@pytest.fixture
def classifier(monkeypatch: pytest.MonkeyPatch) -> _Classifier:
    """Fake only the LLM wire: the promotion classifier and the distiller."""
    import arcmemory.provider as provider

    fake = _Classifier()
    monkeypatch.setattr(
        provider, "_promotion_classifier", lambda cfg, _prompts: fake if cfg is not None else None
    )
    monkeypatch.setattr(provider, "build_distiller", lambda *_a, **_k: _NullDistiller())
    return fake


def _install_memory(arc_home: Path, agent_dir: Path, tmp_path: Path) -> None:
    bundle = arcbundle.build_bundle(
        _SOURCE_CATALOG / "memory",
        module="memory",
        version="1.0.0",
        private_key=_ISSUER_KEYPAIR.private_key,
        issuer=_ISSUER,
        out=tmp_path / "bundles" / "memory",
    )
    verified = arcbundle.verify_bundle(
        bundle, tier="personal", trusted_issuers={_ISSUER: _ISSUER_KEYPAIR.public_key}
    )
    installed = arcbundle.materialize(verified, module_root(arc_home))
    arcbundle.copy_capabilities(installed, agent_dir, module="memory")


def _agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ArcAgent, Path]:
    arc_home, agent_dir, workspace = tmp_path / "arc", tmp_path / "agent", tmp_path / "ws"
    for path in (arc_home, agent_dir, workspace):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_home))
    _install_memory(arc_home, agent_dir, tmp_path)
    config = ArcAgentConfig(
        agent=AgentConfig(
            name="promo-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(key_dir=str(identity_dir(arc_home))),
        security=SecurityConfig(
            operator_key_dir=str(operator_dir(arc_home)),
            validators=ValidatorsConfig(trusted_keys=(_ISSUER_KEYPAIR.public_key.hex(),)),
        ),
        telemetry=TelemetryConfig(enabled=True, export_traces=False),
        modules={
            "memory": ModuleEntry(
                enabled=True,
                config={
                    "brain": "arcmemory",
                    "embed_backend": "none",
                    "promotion": {"enabled": True},
                },
            )
        },
    )
    return ArcAgent(config=config, config_path=agent_dir / "arcagent.toml"), workspace


@asynccontextmanager
async def _started(agent: ArcAgent) -> AsyncIterator[ArcAgent]:
    model = MagicMock()
    model.close = AsyncMock()
    with (
        patch("arcagent.core.model_manager.load_eval_model", return_value=model),
        patch("arcagent.utils.model_helpers.load_eval_model", return_value=model),
    ):
        await agent.startup()
        try:
            yield agent
        finally:
            await agent.shutdown()


def _memory_state(agent: ArcAgent) -> Any:
    runtime = sys.modules.get("arcagent.modules.memory._runtime")
    assert runtime is not None, "memory runtime never loaded — module did not install"
    return runtime.state_for(agent.did)


def _card(workspace: Path, card_id: str) -> None:
    InsightStore(workspace.resolve()).write(
        Insight(id=card_id, statement=f"{card_id}: renewal closes at $42k/yr.", trigger="t")
    )


def _next_night(workspace: Path) -> None:
    (workspace.resolve() / "memory" / _HYGIENE_LAST_NAME).unlink()


def _fleet(agent: ArcAgent, workspace: Path, team_root: Path) -> tuple[Any, Any, Any]:
    from arcteam.shared_knowledge import (
        ComposedSharedKnowledgeAgent,
        FleetSharedKnowledgeComposition,
        FleetSharedKnowledgeService,
    )
    from arcteam.team import Team

    team = Team(id="team:fleet", name="fleet", members=[agent.did], default_channel="channel://f")
    service = FleetSharedKnowledgeService.for_team_root(team_root)
    access = KnowledgeAccess(agent.did, "UNCLASSIFIED")
    member = ComposedSharedKnowledgeAgent(
        agent,
        PersonalKnowledgeAdapter(workspace, agent.did),
        access,
        agent.extension_signer,
        agent.audit_sink,
    )
    return FleetSharedKnowledgeComposition(team, service), service, member


def _chain_actions(agent: ArcAgent) -> str:
    return agent._policy_audit_log_path().read_text(encoding="utf-8")


async def test_fleet_attach_after_start_publishes_durably_and_detach_stops_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, classifier: _Classifier
) -> None:
    agent, workspace = _agent(tmp_path, monkeypatch)
    async with _started(agent):
        brain = _memory_state(agent).brain
        _card(workspace, "acme-renewal")
        composition, service, member = _fleet(agent, workspace, tmp_path / "team")

        await composition.start([member])
        summary = await brain.consolidate()

        assert summary["promotion_status"] == "completed"
        assert summary["promotion_promoted"] == 1
        (document,) = await service.list_documents(member.access)
        assert document.owner_did == agent.did
        chain = _chain_actions(agent)
        assert "knowledge.promotion_decision" in chain
        assert "memory.promotion.egress" in chain

        await composition.stop([member])
        _card(workspace, "globex-renewal")
        _next_night(workspace)
        calls_before = len(classifier.inputs)
        summary = await brain.consolidate()

        assert summary["promotion_status"] == "publisher_unavailable"
        assert len(classifier.inputs) == calls_before


async def test_module_forged_attach_and_detach_are_ignored_and_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, classifier: _Classifier
) -> None:
    agent, workspace = _agent(tmp_path, monkeypatch)
    async with _started(agent):
        composition, _service, member = _fleet(agent, workspace, tmp_path / "team")
        await composition.start([member])
        genuine = _memory_state(agent).shared_knowledge
        assert genuine is not None
        telemetry = agent._telemetry
        assert telemetry is not None
        audited: list[tuple[str, dict[str, Any]]] = []
        real_audit = telemetry.audit_event

        def recording(event_type: str, details: dict[str, Any]) -> None:
            audited.append((event_type, details))
            real_audit(event_type, details)

        monkeypatch.setattr(telemetry, "audit_event", recording)
        bus = agent._bus
        assert bus is not None
        thief = _CapturingPort()

        # Any module holds this bus (it is the ``bus`` dependency every module gets).
        await bus.emit(SHARED_KNOWLEDGE_ATTACHED, {"port": thief}, agent_did=agent.did)
        await bus.emit(SHARED_KNOWLEDGE_DETACHED, {}, agent_did=agent.did)

        assert _memory_state(agent).shared_knowledge is genuine
        forged = [d["event"] for name, d in audited if name == "memory.shared_knowledge_forged"]
        assert forged == [SHARED_KNOWLEDGE_ATTACHED, SHARED_KNOWLEDGE_DETACHED]
        with pytest.raises(RuntimeError, match="already claimed"):
            bus.claim_core_emitter()
