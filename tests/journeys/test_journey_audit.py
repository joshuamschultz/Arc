"""Journey: every audited act names who caused it, inside what, and the chain proves it.

Alpha-2 item 20 (P20-7) — the causal-audit CONTRACT, driven down the paths a user
actually takes. A person messages the agent on a channel; the agent calls a tool;
the operator's Security screen later asks "which run made that call, for whom?".
The answers live in the agent's operator-signed WORM chain and nowhere else, so
every assertion here reads that chain (or the arcui API over it) — never a mock
the test wired up to agree with itself.

Real: the gateway ``SessionRouter`` and ``AsyncioExecutor``, the started
``ArcAgent``, arcrun's loop, the tool registry, the arctrust policy pipeline,
the ``WormSink`` chain, ``StoreIngest`` verification, the arcui auth middleware
and audit routes, and the connection-health probe loop. Faked: the LLM wire
(:class:`ScriptedLLM`) and the provider's answer to a probe.

Concurrency claims force real interleaving with an ``asyncio.Barrier``: an
instant fake would run two "concurrent" turns one after the other and pass even
if attribution lived in a process global (see
``feedback_concurrency_tests_must_interleave``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arcgateway.executor import AsyncioExecutor, InboundEvent
from arcgateway.session import SessionRouter
from arctrust import causal

from .conftest import Deployment, ScriptedLLM, ScriptedTurn

ALICE = "did:arc:user:alice"
BOB = "did:arc:user:bob"
PROBE_TOOL = "journey_probe"


# ---------------------------------------------------------------------------
# Helpers: the agent's WORM chain, a channel message, a recording tool
# ---------------------------------------------------------------------------


def agent_chain(deployment: Deployment) -> Path:
    """The agent's operator-signed policy chain — where ``policy.evaluate`` lands."""
    store = Path(str(deployment.home.parent / "store"))
    return store / "worm" / f"audit-chain-{deployment.agent_name}.jsonl"


def chain_events(chain: Path, action: str | None = None) -> list[dict[str, Any]]:
    """Every record's event on ``chain`` (optionally one action), in seq order."""
    if not chain.exists():
        return []
    events = [json.loads(line)["event"] for line in chain.read_text().splitlines() if line]
    return [e for e in events if action is None or e["action"] == action]


def tool_rows(deployment: Deployment, tool: str) -> list[dict[str, Any]]:
    """The ``policy.evaluate`` rows written for ``tool``, in order."""
    rows = chain_events(agent_chain(deployment), "policy.evaluate")
    return [r for r in rows if r.get("target") == tool]


async def send(router: SessionRouter, agent: Any, *messages: tuple[str, str, str]) -> None:
    """Hand each ``(user_did, chat_id, text)`` to the router and wait for every turn.

    ``SessionRouter.handle`` returns as soon as the turn is handed off, so the
    spawned tasks are awaited here — otherwise an assertion reads an empty chain.
    """
    for user, chat, text in messages:
        await router.handle(
            InboundEvent(
                platform="journey",
                chat_id=chat,
                user_did=user,
                agent_did=agent._identity.did,
                message=text,
            )
        )
    while router._pending_tasks:
        await asyncio.wait_for(
            asyncio.gather(*tuple(router._pending_tasks), return_exceptions=True), 20
        )


@pytest.fixture
def router(agent: Any) -> SessionRouter:
    async def _factory(_did: str) -> Any:
        return agent

    return SessionRouter(executor=AsyncioExecutor(agent_factory=_factory))


@dataclass
class ProbeTool:
    """A read-only tool whose body records the causal context it ran inside.

    Registered on the started agent's real registry, so dispatch, the policy
    pipeline and the WORM write are production code; only the body is the
    test's. ``before`` runs inside the body (a barrier, a background spawn).
    """

    seen: list[tuple[dict[str, Any], causal.CausalContext | None]] = field(default_factory=list)
    before: Callable[[dict[str, Any]], Any] | None = None

    def install(self, agent: Any) -> None:
        from arcagent.core.tool_registry import RegisteredTool, ToolTransport

        async def execute(**args: Any) -> str:
            if self.before is not None:
                await self.before(args)
            self.seen.append((args, causal.current()))
            return "probed"

        agent._tool_registry.register(
            RegisteredTool(
                name=PROBE_TOOL,
                description="Record who is asking.",
                input_schema={"type": "object", "properties": {"who": {"type": "string"}}},
                transport=ToolTransport.NATIVE,
                execute=execute,
                classification="read_only",
            )
        )


# ---------------------------------------------------------------------------
# Contract 1 — a tool call inside a real run carries its whole causal chain
# ---------------------------------------------------------------------------


async def test_a_tool_call_in_a_channel_turn_names_run_call_model_call_agent_and_user(
    agent: Any, router: SessionRouter, scripted_llm: ScriptedLLM, deployment: Deployment
) -> None:
    """Alice asks; the agent calls a tool; its signed policy row says exactly that."""
    probe = ProbeTool()
    probe.install(agent)
    scripted_llm.replies.extend([ScriptedTurn(tool=PROBE_TOOL, args={}), "done"])

    await send(router, agent, (ALICE, "chat-a", "check it"))

    ((_, ctx),) = probe.seen
    assert ctx is not None, "the tool body ran with no causal context bound"
    (row,) = tool_rows(deployment, PROBE_TOOL)
    chain = row["causal"]
    did = agent._identity.did
    assert row["actor_did"] == did, "the agent's tool call was signed as someone else"
    assert chain["initiator"] == "agent"
    assert chain["initiator_id"] == did
    assert chain["on_behalf_of"] == ALICE, "the requesting user is missing from the row"
    assert chain["run_id"] and chain["run_id"] == ctx.run_id
    assert chain["tool_call_id"] and chain["tool_call_id"] == ctx.tool_call_id
    assert chain["llm_call_id"], "the model call that asked for the tool is not named"
    assert chain["llm_call_id"] == ctx.llm_call_id


# ---------------------------------------------------------------------------
# Contract 4 — background isolation and concurrent callers
# ---------------------------------------------------------------------------


def _record_prompts(scripted_llm: ScriptedLLM, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Map each run id the model was called inside to the prompt it was sent.

    Read at the LLM wire, where the run's own context is bound: the one place a
    turn's run id and the user's words are both visible without trusting the
    code under test to report either.
    """
    prompts: dict[str, str] = {}
    invoke = scripted_llm.invoke

    async def recording(messages: list[Any], tools: list[Any] | None = None, **kw: Any) -> Any:
        ctx = causal.current()
        if ctx is not None and ctx.run_id:
            text = "\n".join(str(getattr(m, "content", m)) for m in messages)
            prompts[ctx.run_id] = prompts.get(ctx.run_id, "") + text
        return await invoke(messages, tools, **kw)

    monkeypatch.setattr(scripted_llm, "invoke", recording)
    return prompts


async def test_two_concurrent_turns_from_different_users_never_cross_attribute(
    agent: Any,
    router: SessionRouter,
    scripted_llm: ScriptedLLM,
    deployment: Deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alice and Bob talk to one agent at once; each tool row names its own caller.

    Both tool bodies rendezvous on a barrier, so both turns are provably inside
    tool dispatch at the same moment. A shared (non task-local) binding would
    then hand one turn's principal to the other.
    """
    prompts = _record_prompts(scripted_llm, monkeypatch)
    both_in_flight = asyncio.Barrier(2)

    async def rendezvous(_args: dict[str, Any]) -> None:
        await asyncio.wait_for(both_in_flight.wait(), 10)

    probe = ProbeTool(before=rendezvous)
    probe.install(agent)
    scripted_llm.replies.extend(
        [ScriptedTurn(tool=PROBE_TOOL), ScriptedTurn(tool=PROBE_TOOL), "done", "done"]
    )

    await send(router, agent, (ALICE, "chat-a", "from alice"), (BOB, "chat-b", "from bob"))

    rows = tool_rows(deployment, PROBE_TOOL)
    assert len(rows) == 2 and len(probe.seen) == 2
    expected = {"from alice": ALICE, "from bob": BOB}
    by_call = {ctx.tool_call_id: ctx for _, ctx in probe.seen if ctx is not None}
    for row in rows:
        chain = row["causal"]
        prompt = prompts[chain["run_id"]]
        users = [who for said, who in expected.items() if said in prompt]
        assert len(users) == 1, f"run {chain['run_id']} carried both callers' words: {users}"
        (user,) = users
        assert chain["on_behalf_of"] == user, f"{user}'s tool call was attributed elsewhere"
        assert chain["initiator_id"] == agent._identity.did
        body = by_call[chain["tool_call_id"]]
        assert (body.run_id, body.on_behalf_of) == (chain["run_id"], user)
    assert len({r["causal"]["run_id"] for r in rows}) == 2
    assert len({r["causal"]["request_id"] for r in rows}) == 2


async def test_background_work_spawned_mid_run_never_inherits_the_runs_ids(
    agent: Any, router: SessionRouter, scripted_llm: ScriptedLLM
) -> None:
    """A job a tool starts in the background is its own root, not part of Alice's run.

    The tool body and the background job meet on a barrier, so the job reads its
    context while the run is still inside the tool call — the moment a leaked
    binding would be visible.
    """
    from arcagent.core.config import EvalConfig
    from arcagent.utils.model_helpers import spawn_background

    background: list[causal.CausalContext | None] = []
    tasks: set[asyncio.Task[None]] = set()
    meet = asyncio.Barrier(2)

    async def job() -> None:
        await asyncio.wait_for(meet.wait(), 10)
        background.append(causal.current())

    async def start_job(_args: dict[str, Any]) -> None:
        spawn_background(
            job(),
            background_tasks=tasks,
            semaphore=asyncio.Semaphore(1),
            eval_config=EvalConfig(),
        )
        await asyncio.wait_for(meet.wait(), 10)

    probe = ProbeTool(before=start_job)
    probe.install(agent)
    scripted_llm.replies.extend([ScriptedTurn(tool=PROBE_TOOL), "done"])

    await send(router, agent, (ALICE, "chat-a", "start the job"))
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 10)

    ((_, run_ctx),) = probe.seen
    (job_ctx,) = background
    assert run_ctx is not None and run_ctx.run_id and run_ctx.on_behalf_of == ALICE
    assert job_ctx is not None, "the background job ran with nothing bound"
    leaked = {
        name: getattr(job_ctx, name)
        for name in ("run_id", "tool_call_id", "llm_call_id", "on_behalf_of")
        if getattr(job_ctx, name) is not None
    }
    assert not leaked, f"the background job inherited the run's {leaked}"
    assert job_ctx.request_id != run_ctx.request_id
    assert job_ctx.initiator == "system" and job_ctx.initiator_id != agent._identity.did
