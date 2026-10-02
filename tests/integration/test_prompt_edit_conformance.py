"""SPEC-083 T-1226 (REQ-513, COMP-030) — every prompt edit made in ArcUI reaches the model.

The operator's acceptance check, as two questions:

1. Does every prompt load its text from the ``context/`` folder of its own package?
2. Does ArcUI write an edit into that agent's own override folder, and does each
   package load the agent override first and fall back to the system (stock) prompt?

For EVERY prompt in ``arcprompt.PromptCatalog().catalog()`` (plus the prompts this
spec adds, e.g. ``arcmemory/promotion_classify``) this suite:

* writes an override through ArcUI's real write path — the ``PUT
  /api/agents/{id}/prompts/{package}/{name}`` route, which signs with the operator
  key through ``arcui.prompt_signing`` — into a real agent's
  ``<agent_root>/context/<package>/<name>.md``;
* starts that real agent from its ``arcagent.toml`` (real identity, operator key,
  modules installed by the real ``arc install`` bootstrap);
* drives the prompt's production consumer (the ``DRIVERS`` registry below); and
* asserts the unique marker in the override reached the model wire and the stock
  text it replaced did not.

With no override, the stock text must reach the wire (the fallback half).

The only fakes are the model wires: ``arcllm.load_model`` returns a recording
provider (the seam the journey tests fake), and httpx2's default network transport
answers like TypeSafe's ``POST /v1/systemone`` (the seam the promotion e2e fakes).

A catalog prompt with no registered driver FAILS, so a new prompt cannot skip
conformance. A prompt whose stock text never reaches the wire under its driver fails
with "no production consumer" — the prompt is dead weight in the catalog and must be
wired where its behaviour runs, or deleted with its catalog entry.

The override is built by replacing the text of the stock prompt's longest literal line
with the marker (see :func:`_override_for`), so structured prompts (a YAML rubric, a
strictly parsed classifier question, ``str.format`` templates) keep their shape and a
parser cannot reject the override for an unrelated reason.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from arcprompt import PromptCatalog, PromptMissing, load_stock

_AGENT = "conformance"
_OPERATOR_TOKEN = "operator-token-conformance"
_JEV_KEY = "tsk_conformance_SENTINEL_0000"
#: Inside every agent's nightly memory window (opens 03:00 + <60 min DID offset).
_NIGHT = datetime(2026, 9, 27, 4, 30).astimezone()

#: Prompts this spec requires that may not be packaged yet. They are checked even
#: when absent from the catalog, so their case is RED until the stock file ships.
_REQUIRED_PROMPTS: tuple[tuple[str, str], ...] = (
    ("arcmemory", "promotion_classify"),
    ("arcagent", "messaging_unavailable_note"),
)


# ---------------------------------------------------------------------------
# Fake #1: the LLM wire (``arcllm.load_model``)
# ---------------------------------------------------------------------------


def _flatten(value: Any) -> Iterator[str]:
    """Every string reachable in a message / tool result / JSON body, recursively."""
    if value is None:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, bytes):
        yield value.decode("utf-8", errors="replace")
    elif isinstance(value, dict):
        for item in value.values():
            yield from _flatten(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _flatten(item)
    elif hasattr(value, "model_dump"):
        yield from _flatten(value.model_dump())
    # Numbers/bools/enums are wire metadata, never prompt text.


@dataclass
class Turn:
    """One scripted model turn: plain text, or a tool call the real dispatcher runs."""

    text: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScriptedModel:
    """An arcllm provider that records every request and answers from a script.

    ``turns`` are consumed only by agent-loop calls (a call that offers tools);
    structured JSON calls (distiller) get ``{}`` and single-shot calls get plain text,
    so a driver scripts only the loop turns it cares about.
    """

    turns: list[Turn] = field(default_factory=list)
    requests: list[Any] = field(default_factory=list)
    #: Calls that broke the provider contract (a real provider would reject them).
    violations: list[str] = field(default_factory=list)

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

    async def invoke(self, messages: Any, tools: list[Any] | None = None, **kw: Any) -> Any:
        from arcllm.types import LLMResponse, Message, ToolCall, Usage

        if not isinstance(messages, list) or not all(isinstance(m, Message) for m in messages):
            # A real provider serializes a list of arcllm Messages; anything else
            # never reaches a model. Recorded, not accepted, so a consumer wired to the
            # wrong seam cannot look conformant against a lenient fake.
            self.violations.append(f"invoke(messages={type(messages).__name__})")
            raise TypeError(
                f"provider contract: messages must be list[arcllm.Message], "
                f"got {type(messages).__name__}"
            )
        self.requests.append({"messages": messages, "tools": tools})
        usage = Usage(input_tokens=10, output_tokens=5, total_tokens=15)
        names = [getattr(t, "name", "") for t in tools or []]
        if "select_strategy" in names:
            call = ToolCall(id="select", name="select_strategy", arguments={"strategy": "react"})
            return LLMResponse(
                tool_calls=[call], stop_reason="tool_use", usage=usage, model="scripted/model"
            )
        if kw.get("response_format") is not None:
            return LLMResponse(
                content="{}", stop_reason="end_turn", usage=usage, model="scripted/model"
            )
        turn = self.turns.pop(0) if tools and self.turns else Turn(text="ok")
        if turn.tool:
            call = ToolCall(id=f"call-{len(self.requests)}", name=turn.tool, arguments=turn.args)
            return LLMResponse(
                tool_calls=[call], stop_reason="tool_use", usage=usage, model="scripted/model"
            )
        return LLMResponse(
            content=turn.text, stop_reason="end_turn", usage=usage, model="scripted/model"
        )

    def wire_text(self) -> str:
        return "\n".join(_flatten([r["messages"] for r in self.requests]))


# ---------------------------------------------------------------------------
# Fake #2: the TypeSafe Jev HTTP wire (httpx2's default transport)
# ---------------------------------------------------------------------------


@dataclass
class JevWire:
    requests: list[Any] = field(default_factory=list)

    def handle(self, request: Any) -> Any:
        import httpx2

        self.requests.append(request)
        probabilities = {"company": 0.97, "personal": 0.01, "agent_only": 0.01, "unclear": 0.01}
        return httpx2.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "scope": {
                        "type": "choice",
                        "choice": "company",
                        "probabilities": probabilities,
                        "confidence": 0.97,
                    },
                    "personal_check": {"type": "noul", "noul": 0.02},
                },
                "usage": {"input_tokens": 12, "output_tokens": 0},
            },
            headers={"x-request-id": f"req-{len(self.requests)}"},
        )

    def wire_text(self) -> str:
        return "\n".join(_flatten([json.loads(r.content) for r in self.requests]))


# ---------------------------------------------------------------------------
# The real deployment: arc home, operator key, one agent, ArcUI's prompt routes
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    """One isolated deployment with a started agent and both wires recording."""

    agent_dir: Path
    llm: ScriptedModel
    jev: JevWire
    agent: Any = None

    def wire_text(self) -> str:
        return f"{self.llm.wire_text()}\n{self.jev.wire_text()}"

    async def say(self, text: str, *, strategies: list[str] | None = None) -> None:
        """One real user turn through ``ArcAgent.run``."""
        session = await self.agent.session("conformance")
        async for _event in self.agent.run(text, session=session, allowed_strategies=strategies):
            pass


def _base_toml(agent_dir: Path, extra: str) -> str:
    """The shape ``arc agent create`` writes (operator_key_dir left to arctrust.paths)."""
    return (
        "[agent]\n"
        f"name = '{_AGENT}'\n"
        "org = 'conformance'\n"
        "type = 'executor'\n"
        f"workspace = '{agent_dir / 'workspace'}'\n\n"
        "[llm]\n"
        "model = 'scripted/model'\n\n"
        "[identity]\n"
        'did = ""\n'
        f"key_dir = '{agent_dir / 'keys'}'\n\n"
        "[telemetry]\n"
        "enabled = false\n\n"
        "[security]\n"
        "tier = 'personal'\n"
        "clearance = 'CUI'\n\n"
        "[spawn]\n"
        "enabled = true\n\n"
        f"{extra}"
    )


def _deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml_extra: str) -> Path:
    """Arc home + operator key + one agent (DID minted, enabled modules installed)."""
    home = tmp_path / "arc-home"
    home.mkdir()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(home))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("ARCTEAM_NATS_URL", "nats://127.0.0.1:1")
    # The module catalog `arc install` copies from: the installed arcagent's own tree.
    import arcagent

    monkeypatch.setenv("ARC_MODULE_SOURCE", str(Path(arcagent.__file__).parent / "modules"))
    monkeypatch.setenv("TYPESAFE_API_KEY", _JEV_KEY)

    from arccli.commands.agent.create import _mint_agent_identity
    from arccli.commands.operator import ensure_operator_key
    from arccli.commands.up import agent_states, bootstrap_modules

    ensure_operator_key(home)
    agent_dir = tmp_path / "team" / f"{_AGENT}_agent"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(_base_toml(agent_dir, toml_extra), encoding="utf-8")
    _mint_agent_identity(agent_dir)
    rows = bootstrap_modules(agent_states(tmp_path / "team"))
    refused = [row for row in rows if "REFUSED" in row or "UNREADABLE" in row]
    assert not refused, f"arc install refused a module: {refused}"
    return agent_dir


def _arcui_client(team_root: Path) -> Any:
    """ArcUI's real agent-detail routes behind its real auth middleware."""
    from arcgateway import team_roster
    from arcui.auth import AuthConfig, AuthMiddleware
    from arcui.registry import AgentRegistry
    from arcui.routes.agent_detail import routes as agent_routes
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    auth = AuthConfig({"viewer_token": "viewer-conformance", "operator_token": _OPERATOR_TOKEN})
    app = Starlette(routes=agent_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = AgentRegistry()
    app.state.embedded_agent_cache = None
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    return TestClient(app)


def _write_override_via_arcui(agent_dir: Path, package: str, name: str, body: str) -> None:
    """PUT the edit through ArcUI exactly as the prompt editor does; it must land signed."""
    client = _arcui_client(agent_dir.parent)
    response = client.put(
        f"/api/agents/{_AGENT}/prompts/{package}/{name}",
        json={"content": body},
        headers={"Authorization": f"Bearer {_OPERATOR_TOKEN}"},
    )
    assert response.status_code == 200, (
        f"ArcUI refused to save an override for {package}/{name}: "
        f"{response.status_code} {response.text}"
    )
    overlay = agent_dir / "context" / package / f"{name}.md"
    assert overlay.is_file(), f"ArcUI did not write {overlay} (the agent's own folder)"
    assert Path(f"{overlay}.arcsig").is_file(), f"ArcUI wrote {overlay} without a signature"


@asynccontextmanager
async def _started(harness: Harness) -> AsyncIterator[Harness]:
    import arcagent

    config_path = harness.agent_dir / "arcagent.toml"
    harness.agent = arcagent.ArcAgent(arcagent.load_config(config_path), config_path=config_path)
    await harness.agent.startup()
    try:
        yield harness
    finally:
        await harness.agent.shutdown()


# ---------------------------------------------------------------------------
# Marker override + stock probe
# ---------------------------------------------------------------------------

_LEADING_MARKUP = re.compile(r"^(\s*(?:[-*>#]+\s+|\d+\.\s+)?(?:[A-Za-z_][\w ]*:\s+)?)(.*)$", re.S)
#: Characters an assembler legitimately transforms (``str.format`` placeholders, XML
#: escaping, tag stripping) or that delimit structure (quotes, line breaks). A probe
#: never spans one, so literal stock text is literal on the wire too.
_SEGMENT_BREAK = re.compile(r"[{}<>&`\"'\n]")


def _probe(stock: str) -> tuple[str, str, str]:
    """The stock span the override replaces: (segment, kept prefix, probe text).

    The longest run of stock text that contains no transformed/structural character,
    with any leading bullet, heading or ``key:`` kept aside so the override keeps the
    prompt's structure.
    """
    segments = [seg for seg in _SEGMENT_BREAK.split(stock) if seg.strip()]
    assert segments, "stock prompt has no literal text to probe"

    def text_of(seg: str) -> str:
        match = _LEADING_MARKUP.match(seg)
        assert match is not None
        return match.group(2).strip()

    segment = max(segments, key=lambda seg: len(text_of(seg)))
    match = _LEADING_MARKUP.match(segment)
    assert match is not None
    return segment, match.group(1), text_of(segment)


def _override_for(stock: str, marker: str) -> tuple[str, str]:
    """(override body, the stock text it removes) — structure-preserving.

    Only the probed span's TEXT is replaced; its indentation, list bullet, YAML key,
    quotes and every other line stay, so a strict parser still accepts the override.
    """
    segment, prefix, text = _probe(stock)
    return stock.replace(segment, f"{prefix}{marker}", 1), text


def _stock_body(package: str, name: str) -> str:
    try:
        return load_stock(package, name)
    except PromptMissing:
        pytest.fail(
            f"{package}/{name} has no stock prompt in {package}/context/ — every prompt a "
            f"package sends must ship as {package}/context/{name}.md"
        )


# ---------------------------------------------------------------------------
# Drivers — one per prompt; each drives the prompt's production consumer
# ---------------------------------------------------------------------------

DriverFn = Callable[[Harness], Awaitable[None]]


@dataclass(frozen=True)
class Driver:
    """How to reach one prompt's consumer: extra agent config + the drive itself."""

    entry: str  # the production entry point, for the report
    run: DriverFn
    toml: str = ""
    no_consumer: str = ""  # set when the audit found no production consumer at all
    #: The consumer only runs over ArcStore's PostgreSQL store. Like every PostgreSQL
    #: integration test here, it runs against ``ARCSTORE_TEST_DATABASE_URL`` and is
    #: skipped (never passed) when no test database is provisioned.
    needs_postgres: bool = False


async def _one_turn(h: Harness) -> None:
    await h.say("Summarize the state of the Acme renewal.")


def _pinned(*strategies: str) -> DriverFn:
    async def run(h: Harness) -> None:
        await h.say("Compute 17 * 23.", strategies=list(strategies))

    return run


def _tool_turn(tool: str, args: dict[str, Any]) -> DriverFn:
    """A turn where the model calls ``tool``; the tool's own model call is on the wire."""

    async def run(h: Harness) -> None:
        h.llm.turns.extend([Turn(tool=tool, args=args), Turn(text="done")])
        await h.say(f"please use {tool}")

    return run


async def _dynamic_child(h: Harness) -> None:
    """A dynamic turn whose script starts one child; the child's system message is sent."""
    script = 'result = agent("Summarize the Acme renewal.")\ncomplete(result["output"])\n'
    h.llm.turns.extend(
        [Turn(tool="emit_script", args={"source": script}), Turn(text="Acme renews at 42k.")]
    )
    await h.say("Research the Acme renewal.", strategies=["dynamic"])


async def _compaction(h: Harness) -> None:
    """Enough turns to cross the (tiny) compact threshold; the post-turn summary runs."""
    for i in range(3):
        await h.say(f"Message {i}: the Acme renewal closes at 42k with net-60 terms.")


async def _policy_shutdown_eval(h: Harness) -> None:
    """A turn records the transcript; ``agent:shutdown`` runs the final policy eval."""
    await h.say("Remember that deploys happen on Tuesdays.")
    await h.agent.shutdown()
    h.agent.shutdown = _noop_shutdown  # the harness's own shutdown must not run twice


async def _noop_shutdown() -> None:
    return None


def _memory_runtime() -> Any:
    import sys

    runtime = sys.modules.get("arcagent.modules.memory._runtime")
    assert runtime is not None, "memory runtime never loaded — the module did not install"
    return runtime


def _memory_capabilities(agent: Any) -> Any:
    import sys

    agent_dir = agent._config_path.parent.resolve()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None) or ""
        if hasattr(module, "consolidate_poll_once") and Path(path).resolve().is_relative_to(
            agent_dir
        ):
            return module
    raise AssertionError(f"no memory capabilities loaded from {agent_dir}")


async def _night(h: Harness) -> None:
    """The production nightly poll (``consolidate_poll_once``) from a background task."""
    import asyncio

    from arcmemory.consolidate import _HYGIENE_LAST_NAME

    runtime = _memory_runtime()
    capabilities = _memory_capabilities(h.agent)
    (h.agent.workspace / "memory" / _HYGIENE_LAST_NAME).unlink(missing_ok=True)

    async def poll() -> bool:
        runtime.bind(runtime.state_for(h.agent.did))
        return bool(await capabilities.consolidate_poll_once(now_local=_NIGHT))

    assert await asyncio.create_task(poll()), "nightly poll did not run in the window"


async def _turn_then_night(h: Harness) -> None:
    await h.say("The Acme renewal closes at 42k per year with net-60 terms.")
    await _night(h)


def _brain(h: Harness) -> Any:
    return _memory_runtime().state_for(h.agent.did).brain


def _distiller(h: Harness) -> Any:
    """The distiller of the brain the agent's memory module built.

    Justification (smallest real production function): these distiller calls fire
    only under data conditions (near-duplicate entity cards, a merged procedure with
    repeated steps, a same-name entity group) that take a multi-night corpus to reach
    through ``Brain.consolidate``. The distiller is the production object the agent
    composed through ``build_brain`` — so whatever prompt source the agent hands the
    brain is exactly what these calls use.
    """
    distiller = getattr(_brain(h), "_distiller", None)
    assert distiller is not None, "the agent's brain has no distiller (distill_provider unset?)"
    return distiller


def _entity_refs() -> list[Any]:
    from arcmemory.distill import EntityRef

    return [
        EntityRef(slug=slug, name="Acme", entity_type="company", facts=["deal: renewal"])
        for slug in ("acme", "acme-corp")
    ]


def _distill(method: str, *args_factory: Callable[[], Any]) -> DriverFn:
    async def run(h: Harness) -> None:
        await h.say("The Acme renewal closes at 42k per year with net-60 terms.")
        args = [make() for make in args_factory]
        await getattr(_distiller(h), method)(*args)

    return run


def _events() -> list[Any]:
    from arcmemory.types import Event

    text = "The Acme renewal closes at 42k per year with net-60 terms."
    return [Event(event_id="ev-1", scope="conformance", kind="respond", text=text)]


def _skills_state(h: Harness) -> Any:
    """The skills module's per-agent runtime state, as the agent configured it."""
    import sys

    runtime = sys.modules.get("arcagent.modules.skills._runtime")
    assert runtime is not None, "skills runtime never loaded — the module did not install"
    return runtime.state_for(h.agent.did) if hasattr(runtime, "state_for") else runtime.state()


def _improver(h: Harness) -> Any:
    """The skill improver the agent's skills module composed.

    Justification: a full improver cycle through the agent needs a curated eval
    suite, trace volume over the trigger threshold and a sandboxed eval runner
    (container/VM). The improver object below is the production one, built by the
    skills runtime with whatever prompt resolver the agent hands it.
    """
    from arcskill.improver.improver import ArcSkillImprover

    state = _skills_state(h)

    assert isinstance(state.adapter, ArcSkillImprover), (
        f"skills module selected {type(state.adapter).__name__}, not the arcskill improver"
    )
    return state.adapter


def _trace() -> Any:
    from datetime import UTC

    from arcskill.improver.models import SkillTrace

    return SkillTrace(
        trace_id="t1",
        session_id="s1",
        skill_name="demo",
        skill_version=0,
        turn_number=1,
        started_at=datetime.now(UTC),
    )


def _bundle(name: str, scripts: dict[str, bytes] | None = None) -> Any:
    from arcskill.improver.models import BundleView

    return BundleView(skill_name=name, text=f"# {name}\nDo the thing.", scripts=scripts or {})


async def _judge(h: Harness) -> None:
    from arcskill.improver.evaluator import SkillEvaluator

    improver = _improver(h)
    # Mirrors the one line production runs per optimize pass (ArcSkillImprover prose path).
    evaluator = SkillEvaluator(
        improver._config, llm=improver._llm, prompt_source=improver._prompts
    )
    await evaluator.evaluate("# demo skill\nDo the thing.", [_trace()])


async def _reflect(h: Harness) -> None:
    from arcskill.improver.models import DimensionScore
    from arcskill.improver.mutate import SkillReflector

    improver = _improver(h)
    # Mirrors the one line production runs per optimize pass (ArcSkillImprover prose path).
    reflector = SkillReflector(
        improver._config, llm=improver._llm, prompt_source=improver._prompts
    )
    weak = {"accuracy": DimensionScore(dimension="accuracy", score=1, rationale="wrong step")}
    await reflector.reflect("# demo skill\nDo the thing.", [(_trace(), weak)], 1000)


async def _code_repair(h: Harness) -> None:
    mutator = _improver(h)._mutator  # production-constructed LLMCodeMutator
    current = _bundle("demo", {"scripts/run.py": b"def run():\n    return 1\n"})
    await mutator.propose(kind="code", current=current, failures="AssertionError", insight="")


async def _merge(h: Harness) -> None:
    merger = _improver(h)._merger  # production-constructed LLMSkillMerger
    await merger.propose(a=_bundle("demo-a"), b=_bundle("demo-b"), insight="duplicate skills")


async def _suitegen(h: Harness) -> None:
    skill_dir = h.agent.workspace / "skills" / "demo"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n# demo\nDo the thing.\n")
    from arcskill.improver.codepatch import build_bundle_view
    from arcskill.improver.suitegen import SuiteGenerator

    # The improver builds its suite trigger only when an operator-anchored revision
    # writer exists (adopted anchors are signed revisions). This harness agent has
    # no revision anchor, so drive the production SuiteGenerator with the
    # improver's own LLM seam and prompt source — the prompt path under test.
    improver = _improver(h)
    generator = SuiteGenerator(
        llm=improver._llm,
        runner=improver._eval_runner,
        config=improver._config.suite,
        prompt_source=improver._prompts,
    )
    view = build_bundle_view("demo", skill_dir / "SKILL.md")
    await generator.generate("demo", view)


async def _curated_judge(h: Harness) -> None:
    from arcskill.improver.goldencase import (
        CuratedGoldenCase,
        evaluate_curated_case,
        rubric_digest,
    )

    improver = _improver(h)
    rubric = "The output names the renewal amount."
    case = CuratedGoldenCase(
        case_id="c1",
        skill_name="demo",
        gate_type="judge_rubric",
        rubric=rubric,
        judge_model_id="scripted/model",
        rubric_sha256=rubric_digest(rubric),
    )
    # Mirrors the one line ArcSkillImprover.evaluate_curated runs per curated case.
    await evaluate_curated_case(
        case, "Acme renews at 42k.", judge=improver._llm, prompt_source=improver._prompts
    )


async def _outcome_classifier(h: Harness) -> None:
    """The turn-end outcome classifier the agent's skills module composed.

    Justification (smallest real production function): ``skills_post_plan`` classifies
    only when ``agent:post_plan`` carries the turn's ``messages``, and the arcrun
    ``turn.end`` event it is bridged from carries only ``turn_number`` today — so no
    real turn reaches the classifier yet (reported as an unwired producer). The
    classifier below is the production object the skills runtime built with the
    agent's prompt source and eval invoker; this is the call ``_classify_outcome`` makes.
    """
    classifier = _skills_state(h).outcome_classifier
    assert classifier is not None, "skills module built no outcome classifier"
    await classifier.classify(
        transcript_window=[{"role": "user", "content": "thanks, that worked"}],
        active_skills=["demo"],
        error_counts={},
    )


async def _promotion_sweep(h: Harness) -> None:
    """A distiller-independent company card, then the nightly sweep over a real fleet port."""
    import arcagent
    from arcmemory.adapters import PersonalKnowledgeAdapter
    from arcmemory.stores.insight import InsightStore
    from arcmemory.types import Insight
    from arcteam.shared_knowledge import (
        ComposedSharedKnowledgeAgent,
        FleetSharedKnowledgeComposition,
        FleetSharedKnowledgeService,
    )
    from arcteam.team import Team

    agent = h.agent
    InsightStore(agent.workspace).write(
        Insight(id="acme-renewal", statement="Acme renewals close at 42k on net-60.", trigger="t")
    )
    team = Team(id="team:c", name="c", members=[agent.did], default_channel="channel://c")
    service = FleetSharedKnowledgeService.for_team_root(h.agent_dir.parent)
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
        ]
    )
    await _night(h)
    assert h.jev.requests, "the promotion sweep sent nothing to the classifier"


_MESSAGING_TOML = "[modules.messaging]\nenabled = true\n"
#: A configured fleet URL nothing listens on: the live backend never comes up, so
#: the teams section carries the "messaging unavailable" note.
_MESSAGING_DOWN_TOML = (
    _MESSAGING_TOML + "\n[modules.messaging.config]\nnats_url = 'nats://127.0.0.1:1'\n"
)


async def _channel_router_tiebreak(h: Harness) -> None:
    """The shared-channel router's tiebreak over two candidates.

    Justification (smallest real production function): the tiebreak fires only
    when a channel message's deterministic ranking is close between two published
    teammates, which takes a live fleet with digests. ``_tiebreak`` is the
    production call, over the agent's own messaging state and its ``oneshot_fn``
    (bound at ``agent:ready`` to ``ArcAgent.run_oneshot``) — so whatever prompt
    source the agent hands the module is exactly what reaches the model.
    """
    import sys
    from types import SimpleNamespace

    runtime = sys.modules.get("arcagent.modules.messaging._runtime")
    assert runtime is not None, "messaging runtime never loaded — the module did not install"
    from arcagent.modules.messaging import activation

    candidates = [
        SimpleNamespace(handle="alpha", agent_did="did:arc:c:alpha"),
        SimpleNamespace(handle="beta", agent_did="did:arc:c:beta"),
    ]
    msg = SimpleNamespace(body="Who owns the Acme renewal quote?")
    await activation._tiebreak(runtime.state(), msg, "general", candidates, {})


def _connected_source() -> Any:
    """One granted document source (the external system, not a model wire)."""
    from arcagent.extension.source import SourceDescription, SyncSourcePage

    class DocsSource:
        async def inspect_source(self, request: Any) -> Any:
            return SourceDescription(
                connection_id=request.connection_id,
                source_kind="docs",
                account_id="conformance",
                display_name="Conformance Docs",
            )

        async def list_source_resources(self, request: Any) -> tuple[Any, ...]:
            return ()

        async def select_source_resources(self, request: Any) -> None:
            return None

        async def sync_source(self, request: Any) -> Any:
            return SyncSourcePage(next_checkpoint="done")

        async def fetch_source(self, request: Any) -> Any:
            raise AssertionError("nothing to fetch")

        async def close_source(self) -> None:
            return None

    return DocsSource()


async def _connected_source_turn(h: Harness) -> None:
    """Register a granted source in the agent's catalog, let the agent's own
    connected-data service describe it, then run one turn (``connections`` section)."""
    import asyncio
    import sys

    runtime = sys.modules.get("arcagent.modules.connected_data._runtime")
    assert runtime is not None, "connected_data runtime never loaded — module did not install"
    service = runtime.state().service
    assert service is not None, "connected_data started no service"
    await h.agent._runtime_deps.source_catalog.register("conformance-docs", _connected_source())
    for _ in range(200):
        statuses = await service.list_sources()
        if statuses and statuses[0].description is not None:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError(f"the registered source was never described: {statuses}")
    await _one_turn(h)


def _no_consumer(reason: str) -> Driver:
    async def run(_h: Harness) -> None:
        return None

    return Driver(entry="(none)", run=run, no_consumer=reason)


_MEMORY_TOML = (
    "[modules.memory]\nenabled = true\n\n"
    "[modules.memory.config]\n"
    "brain = 'arcmemory'\n"
    "tier = 'personal'\n"
    "embed_backend = 'none'\n"
    "distill_provider = 'scripted'\n"
    "distill_model = 'scripted-distill'\n\n"
    "[modules.memory.config.dynamics]\n"
    "consolidate_engine = '{engine}'\n\n"
    "[modules.memory.config.promotion]\n"
    "enabled = {promotion}\n"
)


def _memory(engine: str = "pipeline", promotion: bool = False) -> str:
    return _MEMORY_TOML.format(engine=engine, promotion=str(promotion).lower())


_POLICY_TOML = "[modules.policy]\nenabled = true\n\n[modules.policy.config]\n"
_SKILLS_TOML = (
    "[modules.skills]\nenabled = true\n\n[modules.skills.config]\nadapter = 'arcskill'\n"
)
_COMPACT_TOML = "[context]\nmax_tokens = 400\nprune_threshold = 0.01\ncompact_threshold = 0.02\n\n"
_TURN = "ArcAgent.run (one turn)"

DRIVERS: dict[tuple[str, str], Driver] = {
    # --- arcagent ---------------------------------------------------------------
    ("arcagent", "base_system"): Driver(_TURN, _one_turn),
    ("arcagent", "spawn_guidance"): Driver(_TURN + " with [spawn] enabled", _one_turn),
    ("arcagent", "skill_usage_instruction"): Driver(_TURN + " (skills in manifest)", _one_turn),
    ("arcagent", "authoring_guidance"): Driver(
        "ArcAgent.run -> create_tool with AST-rejected source (rejection tool result)",
        _tool_turn("create_tool", {"name": "bad_tool", "source": "import os\neval('1')\n"}),
    ),
    ("arcagent", "planner_system"): Driver(
        "ArcAgent.run -> plan_create tool -> planning decomposer",
        _tool_turn("plan_create", {"goal": "Prepare the Acme renewal quote"}),
        toml="[modules.planning]\nenabled = true\n",
    ),
    ("arcagent", "context_maintainer_system"): Driver(
        "ArcAgent.run -> workpad_update tool -> workpad maintainer",
        _tool_turn("workpad_update", {"notes": "Acme renewal is open."}),
        toml="[modules.workpad]\nenabled = true\n",
    ),
    ("arcagent", "summary_template"): Driver(
        "ArcAgent.run x3 over [context].compact_threshold -> session compaction",
        _compaction,
        toml=_COMPACT_TOML,
    ),
    ("arcagent", "reflection_prompt"): Driver(
        "ArcAgent.run then agent:shutdown -> policy terminal eval (ACE reflector)",
        _policy_shutdown_eval,
        toml=_POLICY_TOML,
    ),
    ("arcagent", "skill_outcome_classifier"): Driver(
        "agent skills module -> OutcomeClassifier (turn-end label)",
        _outcome_classifier,
        toml=_SKILLS_TOML + "classify_outcomes = true\n",
    ),
    ("arcagent", "reflection_grounding_header"): Driver(
        "ArcAgent.run then nightly memory poll -> memory.consolidated -> policy reflect",
        _turn_then_night,
        toml=_POLICY_TOML + "daily_notes_every_turns = 1\n\n" + _memory(),
    ),
    ("arcagent", "messaging_team_section"): Driver(
        _TURN + " with [modules.messaging] (teams section)", _one_turn, toml=_MESSAGING_TOML
    ),
    ("arcagent", "messaging_unavailable_note"): Driver(
        _TURN + " with [modules.messaging] and an unreachable fleet (teams section)",
        _one_turn,
        toml=_MESSAGING_DOWN_TOML,
    ),
    ("arcagent", "messaging_channel_router"): Driver(
        "messaging channel router tiebreak -> agent.run_oneshot",
        _channel_router_tiebreak,
        toml=_MESSAGING_TOML,
    ),
    ("arcagent", "team_handoffs"): Driver(
        _TURN + " with [modules.tasks] (handoffs section)",
        _one_turn,
        toml="[modules.tasks]\nenabled = true\n",
    ),
    ("arcagent", "connected_data_catalog"): Driver(
        "connected source registered -> ArcAgent.run (connections section)",
        _connected_source_turn,
        toml="[modules.connected_data]\nenabled = true\n",
        needs_postgres=True,
    ),
    ("arcagent", "memory_procedure_guidance"): Driver(
        _TURN + " with the memory module live (procedures section)", _one_turn, toml=_memory()
    ),
    ("arcagent", "memory_disabled_note"): Driver(
        _TURN + " with the memory module on brain 'none' (memory_status section)",
        _one_turn,
        toml="[modules.memory]\nenabled = true\n\n[modules.memory.config]\nbrain = 'none'\n",
    ),
    # --- arcrun -----------------------------------------------------------------
    ("arcrun", "strategy_react"): Driver(_TURN, _one_turn),
    ("arcrun", "strategy_select"): Driver(
        _TURN + " (un-pinned) -> select_strategy call", _one_turn
    ),
    ("arcrun", "code_exec_prefix"): Driver(_TURN + " pinned to 'code'", _pinned("code")),
    ("arcrun", "dynamic_authoring"): Driver(_TURN + " pinned to 'dynamic'", _pinned("dynamic")),
    ("arcrun", "dynamic_child_framing"): Driver(
        _TURN + " pinned to 'dynamic' -> emitted script -> agent() child run", _dynamic_child
    ),
    **{
        ("arcrun", f"strategy_{s}"): Driver(f"{_TURN} pinned to {s!r}", _pinned(s))
        for s in ("code", "dynamic", "oneshot", "plan_execute")
    },
    **{
        ("arcrun", f"strategy_{s}_description"): Driver(
            f"{_TURN} allowed ['react', {s!r}] -> select_strategy call", _pinned("react", s)
        )
        for s in ("code", "dynamic", "oneshot", "plan_execute")
    },
    ("arcrun", "strategy_react_description"): Driver(
        f"{_TURN} allowed ['react', 'oneshot'] -> select_strategy call",
        _pinned("react", "oneshot"),
    ),
    # --- arcmemory --------------------------------------------------------------
    ("arcmemory", "consolidate_agent"): Driver(
        "ArcAgent.run then nightly memory poll (agentic engine)",
        _turn_then_night,
        toml=_memory(engine="agentic"),
    ),
    **{
        ("arcmemory", name): Driver(
            "ArcAgent.run then nightly memory poll (pipeline engine)",
            _turn_then_night,
            toml=_memory(),
        )
        for name in ("distill_fact", "distill_insight", "distill_procedure", "distill_event")
    },
    ("arcmemory", "distill_day"): Driver(
        "agent-built brain distiller.summarize_day",
        _distill("summarize_day", _events),
        toml=_memory(),
    ),
    ("arcmemory", "distill_disambiguate"): Driver(
        "agent-built brain distiller.disambiguate_entity",
        _distill("disambiguate_entity", lambda: "Acme", lambda: "organization", lambda: ["acme"]),
        toml=_memory(),
    ),
    ("arcmemory", "consolidate_steps"): Driver(
        "agent-built brain distiller.consolidate_steps",
        _distill("consolidate_steps", lambda: ["Open the quote.", "Open the quote again."]),
        toml=_memory(),
    ),
    ("arcmemory", "distill_find_contradictions"): Driver(
        "agent-built brain distiller.find_contradictions",
        _distill("find_contradictions", _entity_refs),
        toml=_memory(),
    ),
    ("arcmemory", "distill_merge_confirm"): Driver(
        "agent-built brain distiller.confirm_entity_merges",
        _distill("confirm_entity_merges", lambda: [_entity_refs()]),
        toml=_memory(),
    ),
    ("arcmemory", "promotion_classify"): Driver(
        "nightly memory poll -> promotion sweep -> arcllm.classify -> Jev wire",
        _promotion_sweep,
        toml=_memory(promotion=True),
    ),
    # --- arcskill ---------------------------------------------------------------
    ("arcskill", "judge_prompt"): Driver(
        "agent skills module improver -> SkillEvaluator", _judge, toml=_SKILLS_TOML
    ),
    ("arcskill", "judge_rubric"): Driver(
        "agent skills module improver -> SkillEvaluator (rubric)", _judge, toml=_SKILLS_TOML
    ),
    ("arcskill", "reflection_prompt"): Driver(
        "agent skills module improver -> SkillReflector", _reflect, toml=_SKILLS_TOML
    ),
    ("arcskill", "code_repair_prompt"): Driver(
        "agent skills module improver -> LLMCodeMutator", _code_repair, toml=_SKILLS_TOML
    ),
    ("arcskill", "merge_prompt"): Driver(
        "agent skills module improver -> LLMSkillMerger", _merge, toml=_SKILLS_TOML
    ),
    ("arcskill", "suitegen_prompt"): Driver(
        "agent skills module improver -> SuiteGenerator", _suitegen, toml=_SKILLS_TOML
    ),
    ("arcskill", "curated_judge_prompt"): Driver(
        "agent skills module improver -> evaluate_curated (judge_rubric case)",
        _curated_judge,
        toml=_SKILLS_TOML,
    ),
}


# ---------------------------------------------------------------------------
# The conformance cases
# ---------------------------------------------------------------------------


def _cases() -> list[tuple[str, str]]:
    catalog = [(ref.package, ref.name) for ref in PromptCatalog().catalog()]
    return sorted({*catalog, *_REQUIRED_PROMPTS})


_CASES = [pytest.param(pkg, name, id=f"{pkg}/{name}") for pkg, name in _cases()]


@pytest.fixture
def wires(monkeypatch: pytest.MonkeyPatch) -> tuple[ScriptedModel, JevWire]:
    return install_wires(monkeypatch)


def install_wires(monkeypatch: pytest.MonkeyPatch) -> tuple[ScriptedModel, JevWire]:
    """Fake both model wires (arcllm.load_model + the Jev HTTP transport); return them."""
    import arcllm
    import httpx2
    import httpx2._client as client_module

    llm = ScriptedModel()
    jev = JevWire()
    monkeypatch.setattr(arcllm, "load_model", lambda *_a, **_k: llm)
    monkeypatch.setattr(
        client_module, "AsyncHTTPTransport", lambda *_a, **_k: httpx2.MockTransport(jev.handle)
    )
    return llm, jev


def _driver_for(package: str, name: str) -> Driver:
    driver = DRIVERS.get((package, name))
    if driver is None:
        pytest.fail(
            f"{package}/{name} is in the prompt catalog but has no conformance driver — "
            "register its production consumer in DRIVERS so an ArcUI edit is proven to "
            "reach the model"
        )
    if driver.no_consumer:
        pytest.fail(
            f"{package}/{name}: no production consumer — {driver.no_consumer}. Wire the "
            "prompt where its behaviour runs, or delete it with its catalog entry."
        )
    return driver


def _require_postgres_if_needed(driver: Driver, monkeypatch: pytest.MonkeyPatch) -> None:
    if driver.needs_postgres:
        import os

        dsn = os.environ.get("ARCSTORE_TEST_DATABASE_URL")
        if not dsn:
            pytest.skip("ARCSTORE_TEST_DATABASE_URL is required for this prompt's consumer")
        monkeypatch.setenv("ARCSTORE_DATABASE_URL", dsn)


async def _drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wires: tuple[ScriptedModel, JevWire],
    driver: Driver,
    override: tuple[str, str, str] | None,
) -> Harness:
    _require_postgres_if_needed(driver, monkeypatch)
    agent_dir = _deploy(tmp_path, monkeypatch, driver.toml)
    if override is not None:
        _write_override_via_arcui(agent_dir, *override)
    harness = Harness(agent_dir=agent_dir, llm=wires[0], jev=wires[1])
    try:
        async with _started(harness):
            await driver.run(harness)
    finally:
        if harness.llm.violations:
            pytest.fail(
                f"{driver.entry!r} called the model outside the provider contract "
                f"({harness.llm.violations[0]}) — its prompt can never reach a real model"
            )
    return harness


@pytest.mark.parametrize(("package", "name"), _CASES)
async def test_arcui_override_reaches_the_model_wire(
    package: str,
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wires: tuple[ScriptedModel, JevWire],
) -> None:
    """Agent override first: the ArcUI edit is what the model receives; stock is not."""
    driver = _driver_for(package, name)
    stock = _stock_body(package, name)
    marker = f"CONFORMANCE-MARKER-{package}-{name}-{uuid.uuid4().hex[:10]}"
    body, replaced = _override_for(stock, marker)

    harness = await _drive(tmp_path, monkeypatch, wires, driver, (package, name, body))

    wire = harness.wire_text()
    if marker not in wire and replaced not in wire:
        pytest.fail(
            f"{package}/{name}: no production consumer — driving {driver.entry!r} put "
            "neither the override nor the stock text on the model wire"
        )
    assert marker in wire, (
        f"{package}/{name}: marker not on wire — the ArcUI override was ignored and "
        f"{driver.entry!r} sent the stock prompt (stock-only loading)"
    )
    assert replaced not in wire, (
        f"{package}/{name}: stock text still on the wire beside the override ({driver.entry!r})"
    )


@pytest.mark.parametrize(("package", "name"), _CASES)
async def test_no_override_falls_back_to_stock_on_the_wire(
    package: str,
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wires: tuple[ScriptedModel, JevWire],
) -> None:
    """System fallback: with no agent override the packaged stock text reaches the model."""
    driver = _driver_for(package, name)
    stock = _stock_body(package, name)
    _segment, _prefix, probe = _probe(stock)

    harness = await _drive(tmp_path, monkeypatch, wires, driver, None)

    assert probe in harness.wire_text(), (
        f"{package}/{name}: no production consumer — with no override, "
        f"{driver.entry!r} never sent the stock text to the model"
    )


def test_every_driver_names_a_prompt_that_exists_or_is_required() -> None:
    """A deleted prompt must take its driver with it — no stale conformance entries."""
    known = set(_cases())
    stale = sorted(key for key in DRIVERS if key not in known)
    assert not stale, f"drivers registered for prompts that are not in the catalog: {stale}"


async def test_tampered_override_fails_closed_and_never_reaches_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wires: tuple[ScriptedModel, JevWire]
) -> None:
    """Abuse case — direct artifact tampering after ArcUI signed the edit.

    An attacker who can write the agent folder edits the signed override in place.
    The tampered text must never reach the model, and the agent must refuse loudly
    (a broken override never silently degrades to stock — REQ-125).
    """
    from arcprompt import PromptUnsigned

    agent_dir = _deploy(tmp_path, monkeypatch, "")
    stock = _stock_body("arcagent", "base_system")
    signed_marker = f"CONFORMANCE-SIGNED-{uuid.uuid4().hex[:10]}"
    body, _replaced = _override_for(stock, signed_marker)
    _write_override_via_arcui(agent_dir, "arcagent", "base_system", body)
    overlay = agent_dir / "context" / "arcagent" / "base_system.md"
    tampered_marker = f"CONFORMANCE-TAMPERED-{uuid.uuid4().hex[:10]}"
    overlay.write_text(overlay.read_text().replace(signed_marker, tampered_marker))

    harness = Harness(agent_dir=agent_dir, llm=wires[0], jev=wires[1])
    with pytest.raises(PromptUnsigned):
        async with _started(harness):
            await _one_turn(harness)
    assert tampered_marker not in harness.wire_text()


# ---------------------------------------------------------------------------
# One frozen prompt set per run (REQ-123) — module sections included
# ---------------------------------------------------------------------------

#: Prompts that reach the model as a section added by an ``agent:assemble_prompt``
#: handler (a module or the capability manifest), plus the harness base section.
_ASSEMBLED_SECTION_PROMPTS = [
    pytest.param("arcagent", name, id=f"arcagent/{name}")
    for name in (
        "base_system",
        "skill_usage_instruction",
        "messaging_team_section",
        "messaging_unavailable_note",
        "team_handoffs",
        "connected_data_catalog",
        "memory_procedure_guidance",
        "memory_disabled_note",
    )
]


def _edit_on_first_assembly(h: Harness, package: str, name: str, body: str) -> None:
    """Write the override through ArcUI the moment the run starts assembling its prompt.

    The run's snapshot is already frozen at that point, so this is an operator edit
    landing mid-run: no section of THIS run may carry it.
    """
    fired = False

    async def edit(_ctx: Any) -> None:
        nonlocal fired
        if not fired:
            fired = True
            _write_override_via_arcui(h.agent_dir, package, name, body)

    h.agent._bus.subscribe(
        event="agent:assemble_prompt",
        handler=edit,
        priority=1,
        module_name="conformance.mid_run_edit",
    )


@pytest.mark.parametrize(("package", "name"), _ASSEMBLED_SECTION_PROMPTS)
async def test_override_written_mid_run_reaches_only_the_next_run(
    package: str,
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wires: tuple[ScriptedModel, JevWire],
) -> None:
    """Every section of one run comes from that run's frozen snapshot, never a live read."""
    driver = _driver_for(package, name)
    _require_postgres_if_needed(driver, monkeypatch)
    stock = _stock_body(package, name)
    marker = f"CONFORMANCE-MIDRUN-{name}-{uuid.uuid4().hex[:10]}"
    body, replaced = _override_for(stock, marker)
    harness = Harness(
        agent_dir=_deploy(tmp_path, monkeypatch, driver.toml), llm=wires[0], jev=wires[1]
    )

    async with _started(harness):
        _edit_on_first_assembly(harness, package, name, body)
        await driver.run(harness)
        first_run = harness.wire_text()
        harness.llm.requests.clear()
        await driver.run(harness)
        next_run = harness.wire_text()

    assert (harness.agent_dir / "context" / package / f"{name}.md").is_file()
    assert marker not in first_run, (
        f"{package}/{name}: an override written mid-run changed that same run's prompt — "
        "the section was resolved live instead of from the run's frozen snapshot"
    )
    assert replaced in first_run, f"{package}/{name}: stock text missing from the first run"
    assert marker in next_run, f"{package}/{name}: the next run did not pick up the override"


async def test_spawned_child_strategy_guidance_carries_the_operator_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wires: tuple[ScriptedModel, JevWire]
) -> None:
    """A ``spawn_task`` child runs under the parent run's prompts, not the stock set."""
    stock = _stock_body("arcrun", "strategy_react")
    marker = f"CONFORMANCE-CHILD-{uuid.uuid4().hex[:10]}"
    body, replaced = _override_for(stock, marker)
    agent_dir = _deploy(tmp_path, monkeypatch, "")
    _write_override_via_arcui(agent_dir, "arcrun", "strategy_react", body)
    child_task = f"CHILD-TASK-{uuid.uuid4().hex[:8]} list the renewal risks"
    harness = Harness(agent_dir=agent_dir, llm=wires[0], jev=wires[1])
    harness.llm.turns.extend(
        [
            Turn(tool="spawn_task", args={"task": child_task}),
            Turn(text="risk: late signature"),
            Turn(text="done"),
        ]
    )

    async with _started(harness):
        await harness.say("Split the Acme renewal review.", strategies=["react"])

    child_requests = [
        request
        for request in harness.llm.requests
        if any(
            getattr(message, "role", "") == "user" and child_task in "".join(_flatten(message))
            for message in request["messages"]
        )
    ]
    assert child_requests, "the spawn_task child never called the model"
    child_wire = "\n".join(_flatten([r["messages"] for r in child_requests]))
    assert marker in child_wire, "the child ran on stock strategy guidance, not the override"
    assert replaced not in child_wire
