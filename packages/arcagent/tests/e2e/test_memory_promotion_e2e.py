"""SPEC-083 T-1209 — memory promotion end to end, only the Jev HTTP wire faked.

Two REAL agents, each loaded from a real ``arcagent.toml`` (real identity, the
signed memory bundle installed through ``arcbundle``, promotion enabled at the
personal tier), joined into one real team root through the real
``FleetSharedKnowledgeComposition`` exactly as ``arccli`` ``_serve.py`` wires it
(clearance from ``[security].clearance``, the agent's own ``audit_sink``).

What is real, top to bottom: the agent turn that captures what the user said,
the arcllm-backed distiller that mints the insight card, the nightly poll that
drives ``Brain.consolidate()``, the promotion sweep, ``ArcllmPromotionClassifier``,
``arcllm.classify``, the ``arcllm.classifiers.jev`` drop-in, the installed
``typesafe-sdk`` and its ``httpx2`` client, the key resolved from the env var the
config names, the signed fleet store and agent B's ``shared_knowledge_search``
tool dispatched through its governed tool registry.

What is fake: the two LLM wires. ``arcllm.load_model`` returns a scripted model
(the distiller's and the agent loop's provider — the same seam the journey tests
fake), and ``httpx2``'s network transport is a recording ``MockTransport`` that
answers like TypeSafe's ``POST /v1/systemone``. Production passes no transport,
so the default-transport constructor is the one replaced; the SDK builds its
request bytes for real and every one of them is recorded.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import arcbundle
import httpx2
import pytest
import typesafe_sdk  # noqa: F401  # the real SDK is under test: fail loudly if absent
from arcmemory.consolidate import _HYGIENE_LAST_NAME
from arcprompt import load_stock
from arctrust import generate_keypair
from arctrust.paths import module_root, operator_dir

import arcagent

_SOURCE_CATALOG = Path(__file__).resolve().parents[2] / "src" / "arcagent" / "modules"
_ISSUER = "did:arc:test-operator"
_ISSUER_KEYPAIR = generate_keypair()

_KEY = "tsk_e2e_SENTINEL_KEY_5e6f7a8b"
_ALT_KEY = "tsk_alt_SENTINEL_KEY_9c0d1e2f"
_CLEARANCE = "CUI"
#: Inside every agent's nightly window (opens 03:00 + <60 min DID offset, 3h long).
_NIGHT = datetime(2026, 9, 27, 4, 30).astimezone()

_COMPANY_SAID = "Acme renewal closes at 42k per year with net-60 terms."
_COMPANY_CARD = "Acme renewals close at 42k/yr on net-60 terms; quote net-60 up front."
_PERSONAL_MARKER = "PERSONAL-MARKER-7731"
_PERSONAL_SAID = f"My daughter's recital is Friday at six. {_PERSONAL_MARKER}"
_PERSONAL_CARD = f"The operator's daughter has a recital on Fridays at six. {_PERSONAL_MARKER}"
_SECRET_VALUE = "hunter2-SENTINEL-SECRET-4412"
_SECRET_SAID = f"The staging deploy password is {_SECRET_VALUE}."
_SECRET_CARD = f"Staging deploys use the password {_SECRET_VALUE}."

#: What the user said -> the insight card the (scripted) distiller mints from it.
_MINTS = {
    _COMPANY_SAID: ("acme-renewal-terms", _COMPANY_CARD),
    _PERSONAL_SAID: ("daughter-recital", _PERSONAL_CARD),
    _SECRET_SAID: ("staging-deploy-access", _SECRET_CARD),
}


# ---------------------------------------------------------------------------
# Fake #1: the TypeSafe HTTP wire (everything above it is real)
# ---------------------------------------------------------------------------


def _systemone_json(state: str) -> dict[str, Any]:
    personal = _PERSONAL_MARKER in state
    label = "personal" if personal else "company"
    others = [k for k in ("company", "personal", "agent_only", "unclear") if k != label]
    probabilities = {label: 0.985, **dict.fromkeys(others, 0.005)}
    return {
        "model": "jev-1.13.0",
        "answers": {
            "scope": {
                "type": "choice",
                "choice": label,
                "probabilities": probabilities,
                "confidence": 0.98,
            },
            "personal_check": {"type": "noul", "noul": 0.97 if personal else 0.02},
        },
        "usage": {"input_tokens": 41, "output_tokens": 0},
    }


@dataclass
class JevWire:
    """Records every request the real SDK puts on the wire and answers it."""

    requests: list[Any] = field(default_factory=list)

    def handle(self, request: Any) -> Any:
        self.requests.append(request)
        state = json.loads(request.content)["state"]
        return httpx2.Response(
            200, json=_systemone_json(state), headers={"x-request-id": f"req-{len(self.requests)}"}
        )

    @property
    def sent_bytes(self) -> bytes:
        return b"".join(bytes(request.content) for request in self.requests)

    def states(self) -> list[str]:
        return [json.loads(request.content)["state"] for request in self.requests]


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> JevWire:
    """Replace only the network transport httpx2 builds when none is passed."""
    import httpx2._client as client_module

    recorder = JevWire()
    monkeypatch.setattr(
        client_module,
        "AsyncHTTPTransport",
        lambda *_a, **_k: httpx2.MockTransport(recorder.handle),
    )
    return recorder


# ---------------------------------------------------------------------------
# Fake #2: the LLM wire (distiller + agent loop provider)
# ---------------------------------------------------------------------------


@dataclass
class ScriptedModel:
    """An arcllm provider: JSON distiller answers + queued agent-loop turns."""

    turns: list[dict[str, Any] | str] = field(default_factory=list)
    calls: list[list[Any]] = field(default_factory=list)
    minted: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def model_name(self) -> str:
        return "scripted/model"

    def validate_config(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def invoke(self, messages: list[Any], tools: list[Any] | None = None, **kw: Any) -> Any:
        from arcllm.types import LLMResponse, ToolCall, Usage

        self.calls.append(list(messages))
        usage = Usage(input_tokens=10, output_tokens=5, total_tokens=15)
        if kw.get("response_format") is not None:
            return LLMResponse(
                content=json.dumps(self._distill(messages)),
                model=self.model_name,
                stop_reason="end_turn",
                usage=usage,
            )
        if tools and any(getattr(t, "name", "") == "select_strategy" for t in tools):
            call = ToolCall(id="select", name="select_strategy", arguments={"strategy": "react"})
            return LLMResponse(
                tool_calls=[call], model=self.model_name, stop_reason="tool_use", usage=usage
            )
        turn = self.turns.pop(0) if self.turns else "Noted."
        if isinstance(turn, dict):
            call = ToolCall(
                id=f"call-{len(self.calls)}", name=turn["tool"], arguments=turn["args"]
            )
            return LLMResponse(
                tool_calls=[call], model=self.model_name, stop_reason="tool_use", usage=usage
            )
        return LLMResponse(
            content=turn, model=self.model_name, stop_reason="end_turn", usage=usage
        )

    def _distill(self, messages: list[Any]) -> dict[str, Any]:
        system, user = str(messages[0].content), str(messages[1].content)
        if system != load_stock("arcmemory", "distill_insight"):
            return {}
        insights = []
        for said, (card_id, statement) in _MINTS.items():
            if said in user:
                self.minted.append(card_id)
                insights.append(
                    {
                        "id": card_id,
                        "statement": statement,
                        "trigger": "a recurring commitment is stated",
                        "cues": ["recurring-commitment"],
                        "instances": [],
                    }
                )
        return {"insights": insights}


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> ScriptedModel:
    import arcllm

    model = ScriptedModel()
    monkeypatch.setattr(arcllm, "load_model", lambda *_a, **_k: model)
    return model


# ---------------------------------------------------------------------------
# The real deployment: one arc home, one team root, agents from real TOML
# ---------------------------------------------------------------------------


@dataclass
class Deployment:
    home: Path
    team_root: Path
    installed_memory: Path


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Deployment]:
    home, team_root = tmp_path / "arc-home", tmp_path / "team"
    home.mkdir()
    team_root.mkdir()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(home))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("TYPESAFE_API_KEY", _KEY)
    monkeypatch.delenv("TYPESAFE_KEY_ALT", raising=False)
    # Ambient steering must never move egress: the plugin pins host + model.
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://attacker.example.test")
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
    yield Deployment(home, team_root, arcbundle.materialize(verified, module_root(home)))


def _agent_toml(
    deployment: Deployment,
    agent_dir: Path,
    *,
    name: str,
    tier: str,
    memory_tier: str | None,
    promotion_extra: str,
    promotion_enabled: bool = True,
) -> str:
    memory_tier_line = f'tier = "{memory_tier}"\n' if memory_tier is not None else ""
    return (
        "[agent]\n"
        f'name = "{name}"\n'
        'org = "e2e"\n'
        'type = "executor"\n'
        f'workspace = "{agent_dir / "workspace"}"\n\n'
        "[llm]\n"
        'model = "scripted/model"\n\n'
        "[identity]\n"
        f'key_dir = "{agent_dir / "keys"}"\n\n'
        "[security]\n"
        f'tier = "{tier}"\n'
        f'clearance = "{_CLEARANCE}"\n'
        f'operator_key_dir = "{operator_dir(deployment.home)}"\n\n'
        "[security.validators]\n"
        f'trusted_keys = ["{_ISSUER_KEYPAIR.public_key.hex()}"]\n\n'
        "[telemetry]\n"
        "enabled = true\n"
        "export_traces = false\n\n"
        "[modules.memory]\n"
        "enabled = true\n\n"
        "[modules.memory.config]\n"
        'brain = "arcmemory"\n'
        f"{memory_tier_line}"
        'embed_backend = "none"\n'
        'distill_provider = "scripted"\n'
        'distill_model = "scripted-distill"\n\n'
        "[modules.memory.config.dynamics]\n"
        'consolidate_engine = "pipeline"\n\n'
        "[modules.memory.config.promotion]\n"
        f"enabled = {str(promotion_enabled).lower()}\n"
        f"{promotion_extra}"
    )


def _agent(
    deployment: Deployment,
    name: str,
    *,
    tier: str = "personal",
    memory_tier: str | None = "personal",
    promotion_extra: str = "",
    promotion_enabled: bool = True,
) -> arcagent.ArcAgent:
    agent_dir = deployment.team_root / f"{name}_agent"
    (agent_dir / "workspace").mkdir(parents=True)
    config_path = agent_dir / "arcagent.toml"
    config_path.write_text(
        _agent_toml(
            deployment,
            agent_dir,
            name=name,
            tier=tier,
            memory_tier=memory_tier,
            promotion_extra=promotion_extra,
            promotion_enabled=promotion_enabled,
        ),
        encoding="utf-8",
    )
    arcbundle.copy_capabilities(deployment.installed_memory, agent_dir, module="memory")
    return arcagent.ArcAgent(arcagent.load_config(config_path), config_path=config_path)


@asynccontextmanager
async def _fleet(deployment: Deployment, *agents: arcagent.ArcAgent) -> AsyncIterator[Any]:
    """Start the agents, then compose shared knowledge exactly as ``_serve.py`` does."""
    from arcmemory.adapters import PersonalKnowledgeAdapter
    from arcteam.shared_knowledge import (
        ComposedSharedKnowledgeAgent,
        FleetSharedKnowledgeComposition,
        FleetSharedKnowledgeService,
    )
    from arcteam.team import Team

    started: list[arcagent.ArcAgent] = []
    try:
        for agent in agents:
            await agent.startup()
            started.append(agent)
        team = Team(
            id="team:fleet",
            name="fleet",
            members=[agent.did for agent in agents],
            default_channel="channel://fleet",
        )
        service = FleetSharedKnowledgeService.for_team_root(deployment.team_root)
        composition = FleetSharedKnowledgeComposition(team, service)
        await composition.start(
            [
                ComposedSharedKnowledgeAgent(
                    agent,
                    PersonalKnowledgeAdapter(agent.workspace, agent.did),
                    arcagent.KnowledgeAccess(agent.did, agent._config.security.clearance),
                    agent.extension_signer,
                    agent.audit_sink,
                )
                for agent in agents
            ]
        )
        yield service
    finally:
        for agent in reversed(started):
            await agent.shutdown()


def _runtime() -> Any:
    runtime = sys.modules.get("arcagent.modules.memory._runtime")
    assert runtime is not None, "memory runtime never loaded — module did not install"
    return runtime


def _loaded_capabilities(agent: arcagent.ArcAgent) -> Any:
    """The memory capability module the agent loaded from its signed install."""
    agent_dir = agent._config_path.parent.resolve()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None) or ""
        if hasattr(module, "consolidate_poll_once") and Path(path).resolve().is_relative_to(
            agent_dir
        ):
            return module
    raise AssertionError(f"no memory capabilities loaded from {agent_dir}")


async def _say(agent: arcagent.ArcAgent, text: str) -> str:
    """One real user turn (capture hooks run for real)."""
    session = await agent.session("e2e")
    out: list[str] = []
    async for event in agent.run(text, session=session):
        piece = getattr(event, "text", None)
        if isinstance(piece, str):
            out.append(piece)
    return "".join(out)


async def _night(agent: arcagent.ArcAgent) -> dict[str, Any]:
    """The production nightly poll for ``agent``, from a background task.

    Returns the ``memory.consolidated`` payload the poll publishes on the bus.
    A night already run today is forgotten first, so each call is a new night.
    """
    runtime = _runtime()
    capabilities = _loaded_capabilities(agent)
    (agent.workspace / "memory" / _HYGIENE_LAST_NAME).unlink(missing_ok=True)
    published: list[dict[str, Any]] = []

    async def record(ctx: Any) -> None:
        published.append(dict(ctx.data))

    bus = agent._bus
    assert bus is not None
    token = bus.subscribe("memory.consolidated", record)

    async def poll() -> bool:
        runtime.bind(runtime.state_for(agent.did))
        return bool(await capabilities.consolidate_poll_once(now_local=_NIGHT))

    try:
        assert await asyncio.create_task(poll()), "nightly poll did not run in the window"
    finally:
        bus.unsubscribe(token)
    assert len(published) == 1
    return published[0]


async def _search_as(agent: arcagent.ArcAgent, llm: ScriptedModel, query: str) -> str:
    """Agent ``agent`` calls ``shared_knowledge_search`` in a real turn; the tool's text."""
    llm.turns.extend([{"tool": "shared_knowledge_search", "args": {"query": query}}, "Done."])
    before = len(llm.calls)
    await _say(agent, f"search shared knowledge for {query}")
    later = llm.calls[before:]
    tool_text = [
        str(getattr(m, "content", m))
        for call in later
        for m in call
        if getattr(m, "role", "") == "tool"
    ]
    assert tool_text, "shared_knowledge_search never returned a result to the loop"
    return tool_text[-1]


def _chain(agent: arcagent.ArcAgent) -> str:
    worm = agent._policy_worm
    assert worm is not None
    assert worm.verify_chain(), "the agent's signed WORM chain does not verify"
    return agent._policy_audit_log_path().read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T-1209 — personal tier, two agents, the default key env
# ---------------------------------------------------------------------------


async def test_company_insight_reaches_peer_and_nothing_else_leaves(
    deployment: Deployment, wire: JevWire, llm: ScriptedModel
) -> None:
    alice = _agent(deployment, "alice")
    bob = _agent(deployment, "bob")
    async with _fleet(deployment, alice, bob) as service:
        # Night 1: a company fact and a personal fact, both distiller-minted.
        await _say(alice, _COMPANY_SAID)
        await _say(alice, _PERSONAL_SAID)
        night1 = await _night(alice)

        assert sorted(llm.minted) == ["acme-renewal-terms", "daughter-recital"]
        assert night1["promotion_status"] == "completed"
        assert night1["insights_minted"] == 2
        assert len(wire.requests) == 2
        for request in wire.requests:
            assert request.method == "POST"
            assert request.url.host == "api.typesafe.ai"
            assert request.url.path == "/v1/systemone"
            assert request.headers["authorization"] == f"Bearer {_KEY}"
            assert json.loads(request.content)["model"] == "jev-1.13.0"
        assert any(_COMPANY_CARD in state for state in wire.states())
        # Only memory text crosses the wire — no DID rides along.
        assert alice.did.encode() not in wire.sent_bytes

        # Bob finds Alice's company card, attributed to Alice at Alice's clearance.
        found = await _search_as(bob, llm, "Acme")
        (document,) = await service.list_documents(arcagent.KnowledgeAccess(bob.did, _CLEARANCE))
        assert document.reference.identifier in found
        assert "42k" in found
        assert document.owner_did == alice.did
        assert document.classification == _CLEARANCE
        # The personal card is not in the fleet store.
        assert "No shared knowledge results found." in await _search_as(bob, llm, "recital")
        assert _PERSONAL_MARKER not in f"{document.title} {document.excerpt}"

        # Egress + decision records are in Alice's signed WORM chain.
        chain = _chain(alice)
        assert "memory.promotion.egress" in chain
        assert "knowledge.promotion_decision" in chain

        # Night 2: nothing new -> zero requests.
        night2 = await _night(alice)
        assert night2["promotion_status"] == "completed"
        assert len(wire.requests) == 2

        # Night 3: a secret-bearing card -> zero bytes leave the box.
        await _say(alice, _SECRET_SAID)
        sent_before = len(wire.requests)
        await _night(alice)
        assert llm.minted[-1] == "staging-deploy-access"
        assert len(wire.requests) == sent_before
        assert _SECRET_VALUE.encode() not in wire.sent_bytes
        assert (
            len(await service.list_documents(arcagent.KnowledgeAccess(bob.did, _CLEARANCE))) == 1
        )


# ---------------------------------------------------------------------------
# T-1210 gap — the configured key env must reach the classifier
# ---------------------------------------------------------------------------


async def test_non_default_api_key_env_from_config_is_the_key_on_the_wire(
    deployment: Deployment, wire: JevWire, llm: ScriptedModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_KEY_ALT", _ALT_KEY)
    alice = _agent(deployment, "alice", promotion_extra='api_key_env = "TYPESAFE_KEY_ALT"\n')
    bob = _agent(deployment, "bob")
    async with _fleet(deployment, alice, bob) as service:
        await _say(alice, _COMPANY_SAID)
        night = await _night(alice)

        assert night["promotion_status"] == "completed"
        assert len(wire.requests) == 1
        assert wire.requests[0].headers["authorization"] == f"Bearer {_ALT_KEY}"
        (document,) = await service.list_documents(arcagent.KnowledgeAccess(bob.did, _CLEARANCE))
        assert document.owner_did == alice.did


# ---------------------------------------------------------------------------
# Federal — refused by config; a force-composed sweep sends nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "memory_tier",
    [
        pytest.param("federal", id="rendered-federal-config"),
        # A hand-edited block that omits the module tier must not fall back to
        # "personal" while [security] says federal.
        pytest.param(None, id="memory-block-without-tier"),
    ],
)
async def test_federal_agent_refuses_to_enable_promotion(
    deployment: Deployment, wire: JevWire, llm: ScriptedModel, memory_tier: str | None
) -> None:
    """A federal agent cannot boot on this box (no FIPS provider / vault custody),
    so the memory module is configured exactly as ``configure_module_runtimes``
    would: the signed install's runtime, handed the dependency menu filtered by its
    own ``configure`` signature (``RuntimeDependencies.select_for``)."""
    alice = _agent(deployment, "alice", tier="federal", memory_tier=memory_tier)
    registered = set(_runtime()._registry)
    with pytest.raises(ValueError, match="promotion") as refused:
        _configure_memory_as_lifecycle(alice)
    assert "federal" in str(refused.value)
    assert set(_runtime()._registry) == registered  # refused before any state exists
    assert wire.requests == []


async def test_federal_agent_without_promotion_still_configures_memory(
    deployment: Deployment, wire: JevWire, llm: ScriptedModel
) -> None:
    """Narrowness: the lock refuses promotion, not a federal memory module."""
    alice = _agent(deployment, "alice", tier="federal", memory_tier=None, promotion_enabled=False)
    did = _configure_memory_as_lifecycle(alice)
    assert _runtime().state_for(did).config.promotion.enabled is False


def _configure_memory_as_lifecycle(agent: arcagent.ArcAgent) -> str:
    """Configure the installed memory runtime as ``configure_module_runtimes`` does."""
    from arctrust import AgentIdentity

    from arcagent.core.agent_lifecycle import load_module_runtime

    config = agent._config
    assert config.security.tier == "federal"
    assert config.security.require_fips is True  # a genuine federal config
    identity = AgentIdentity.generate("e2e", "federal")
    runtime = load_module_runtime("memory")
    menu: dict[str, Any] = {
        "config": config.modules["memory"].config,
        "workspace": Path(config.agent.workspace),
        "agent_name": config.agent.name,
        "agent_did": identity.did,
        "identity": identity,
        "tier": str(config.security.tier),
        "telemetry": None,
        "bus": None,
        "policy_pipeline": None,
        "audit_sink": None,
    }
    wanted = inspect.signature(runtime.configure).parameters
    runtime.configure(**{name: value for name, value in menu.items() if name in wanted})
    return identity.did


async def test_force_composed_federal_sweep_sends_nothing(
    deployment: Deployment, wire: JevWire, llm: ScriptedModel, tmp_path: Path
) -> None:
    from arcmemory.adapters.memory_export import ConsolidatedMemoryExporter
    from arcmemory.brain import ArcMemoryBrain
    from arcmemory.config import MemoryConfig
    from arcmemory.promotion.arcllm_classifier import ArcllmPromotionClassifier
    from arcmemory.promotion.config import PromotionConfig
    from arcmemory.provider import build_distiller
    from arcmemory.stores.insight import InsightStore
    from arcmemory.types import Insight
    from arcteam.shared_knowledge import FleetSharedKnowledgeService
    from arcteam.shared_knowledge.port import FleetSharedKnowledgePort
    from arctrust import AgentIdentity

    from arcagent.modules.memory.promotion import SharedKnowledgePublisher

    identity = AgentIdentity.generate("e2e", "federal")
    workspace = (tmp_path / "federal-ws").resolve()
    workspace.mkdir()
    InsightStore(workspace).write(
        Insight(id="acme-renewal-terms", statement=_COMPANY_CARD, trigger="t")
    )
    access = arcagent.KnowledgeAccess(identity.did, "UNCLASSIFIED")
    service = FleetSharedKnowledgeService.for_team_root(deployment.team_root)
    publisher = SharedKnowledgePublisher(
        port=FleetSharedKnowledgePort(service, access=access, signer=identity),
        # Same structural-Protocol bridge the memory runtime uses (_promotion_publisher).
        exporter=cast(Any, ConsolidatedMemoryExporter.for_workspace(workspace, identity.did)),
        access_factory=lambda: access,
    )
    # Bypass every config lock on purpose: an enabled config, a live classifier and
    # a live publisher handed straight to a federal brain.
    brain = ArcMemoryBrain(
        workspace,
        identity.did,
        config=MemoryConfig.for_tier("federal"),
        distiller=build_distiller("scripted", "m", identity.did),
        identity=identity,
        promotion_config=PromotionConfig(enabled=True),
        promotion_classifier=ArcllmPromotionClassifier("jev", "jev-1.13.0", 10.0),
        promotion_publisher=publisher,
        promotion_signer=identity,
    )

    summary = await brain.consolidate()

    assert summary["promotion_status"] == "tier_forbidden"
    assert wire.requests == []
    assert await service.list_documents(access) == []


# ---------------------------------------------------------------------------
# T-1225 — "Run now": backfill existing memory once, then only new items
# ---------------------------------------------------------------------------


def _existing_memory(agent: arcagent.ArcAgent) -> None:
    """Distilled cards already in the agent's (owned) workspace, never yet judged.

    Written through the card store the distiller writes through, after start-up
    (the workspace must already be owned by this agent's DID).
    """
    from arcmemory.stores.insight import InsightStore
    from arcmemory.types import Insight

    store = InsightStore(agent.workspace)
    store.write(Insight(id="acme-renewal-terms", statement=_COMPANY_CARD, trigger="a renewal"))
    store.write(Insight(id="daughter-recital", statement=_PERSONAL_CARD, trigger="a recital"))


async def test_run_now_backfills_existing_memory_then_sends_only_new_items(
    deployment: Deployment, wire: JevWire, llm: ScriptedModel
) -> None:
    alice = _agent(deployment, "alice")
    bob = _agent(deployment, "bob")
    async with _fleet(deployment, alice, bob) as service:
        _existing_memory(alice)
        # The operator's "Run now": the running agent's public entry, no night needed.
        result = await alice.run_memory_promotion()

        assert dict(result) == {
            "status": "completed",
            "evaluated": 2,
            "promoted": 1,
            "kept_private": 1,
            "blocked_secret": 0,
            "too_large": 0,
            "deferred": 0,
        }
        assert len(wire.requests) == 2
        assert all(r.headers["authorization"] == f"Bearer {_KEY}" for r in wire.requests)
        # A manual run never stamps (or skips) that night's hygiene.
        assert not (alice.workspace / "memory" / _HYGIENE_LAST_NAME).exists()

        # Bob finds the promoted company card; the personal one stayed private.
        found = await _search_as(bob, llm, "Acme")
        (document,) = await service.list_documents(arcagent.KnowledgeAccess(bob.did, _CLEARANCE))
        assert document.owner_did == alice.did
        assert document.reference.identifier in found
        assert "42k" in found
        assert "No shared knowledge results found." in await _search_as(bob, llm, "recital")

        # A second Run now right after: every item already judged -> zero requests.
        again = await alice.run_memory_promotion()
        assert again["status"] == "completed"
        assert again["evaluated"] == 0
        assert len(wire.requests) == 2

        # That night: nothing new -> the nightly sweep sends nothing either.
        night = await _night(alice)
        assert night["promotion_status"] == "completed"
        assert len(wire.requests) == 2
        assert "memory.promotion.egress" in _chain(alice)


async def test_run_now_cap_is_bounded_and_this_run_only(
    deployment: Deployment, wire: JevWire, llm: ScriptedModel
) -> None:
    alice = _agent(deployment, "alice")
    bob = _agent(deployment, "bob")
    async with _fleet(deployment, alice, bob):
        _existing_memory(alice)
        with pytest.raises(ValueError):
            await alice.run_memory_promotion(max_items=arcagent.MEMORY_PROMOTION_MAX_ITEMS + 1)
        assert wire.requests == []

        capped = await alice.run_memory_promotion(max_items=1)

        assert capped["evaluated"] == 1
        assert capped["deferred"] == 1
        assert len(wire.requests) == 1


async def test_run_now_on_an_agent_that_is_not_started_is_unavailable(
    deployment: Deployment, wire: JevWire, llm: ScriptedModel
) -> None:
    alice = _agent(deployment, "alice")

    with pytest.raises(arcagent.CapabilityUnavailableError):
        await alice.run_memory_promotion()
    assert wire.requests == []
