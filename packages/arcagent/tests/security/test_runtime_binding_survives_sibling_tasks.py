"""Task 27 follow-up (hotfix) — PERMANENT regression + security test.

``SessionRouter.handle()`` spawns a brand-new, SIBLING ``asyncio.Task`` for
EVERY inbound turn. ``contextvars.ContextVar`` values set in one task are
never visible to a sibling task created independently later — only to
children spawned from within the SAME task. An agent's first-ever turn
(cache miss) happens to run ``ArcAgent.startup()`` (and thus every
``_runtime.configure()`` call) inside that turn's own task, so turn 1
works. But the agent is then cached (mirroring
``arcui.embedded_agents._BoundedAgentCache``), and every SUBSEQUENT turn
gets a fresh sibling task — without the build/bind split, ``configure()``
never re-runs and every builtin/module tool call raises "not configured"
starting on message 2, forever.

This exact task-topology blind spot is why ~2400 unit tests (which always
call ``configure()`` and the tool function within the SAME test
coroutine/task) never caught it. These tests drive the REAL
``SessionRouter`` + ``AsyncioExecutor`` + a caching ``agent_factory``
(mirroring ``bootstrap._make_agent_factory`` + ``embedded_agents``) with
REAL ``ArcAgent`` instances — only the LLM call itself is mocked. Each
agent is started ONCE, directly, in the test function's own task (never
re-started inside a SessionRouter-spawned task), then pre-seeded into the
factory's cache — so EVERY ``router.handle()`` call below exercises a
task that never itself ran ``configure()``, exactly matching turn 2+ in
real production.

Two invariants must hold TOGETHER, and both are pinned here:
  (a) REGRESSION — same agent, two turns via SessionRouter, both in
      sibling tasks relative to the task that started the agent: both
      must work (the bug this file was created to close).
  (b) SECURITY — two agents' turns interleaved through SessionRouter:
      each turn must see ONLY its own agent's state (the task-27/b884f71
      guarantee must survive the hotfix — see
      test_multi_agent_runtime_isolation.py for the original, still-
      passing, same-task interleaving proof).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from arcgateway.executor import AsyncioExecutor, InboundEvent
from arcgateway.session import SessionRouter
from arcrun import TokenEvent, TurnEndEvent

from arcagent.builtins.capabilities import _runtime as builtin_runtime
from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)

# Each recorded turn is either {"workspace": ..., "identity_did": ...} or,
# on the pre-fix RuntimeError, {"error": ...} — always a flat str->str map.
_Observation = dict[str, str]


def _agent_config(name: str, workspace: Path, key_dir: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name=name, org="testorg", type="executor", workspace=str(workspace)),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(key_dir), vault_path=""),
        telemetry=TelemetryConfig(enabled=True),
        context=ContextConfig(max_tokens=10000),
    )


async def _started_agent(config: ArcAgentConfig) -> ArcAgent:
    """Build and start a real ArcAgent in its OWN isolated asyncio.Task.

    Critical: startup() internally calls builtin_runtime.configure(),
    which binds the CURRENT task's ContextVar. If two agents were started
    sequentially in the SAME caller task (e.g. the test function's own
    task), the second agent's configure() would silently overwrite the
    first's binding in that shared task — exactly the task-27 bug, just
    relocated into the test's own setup code. Isolating each agent's
    startup into its own task (which then ENDS) keeps each agent's bound
    state scoped to ONLY that now-completed task, so it can never leak
    into whatever task calls SessionRouter.handle() afterward.
    """

    async def _do() -> ArcAgent:
        agent = ArcAgent(config=config)
        with patch("arcagent.core.agent_dispatch.arcrun_run_stream"):
            await agent.startup()
        return agent

    return await asyncio.create_task(_do())


def _fake_run_stream_recording(observed: dict[str, _Observation], key: str) -> Callable[..., Any]:
    """Build an arcrun_run_stream stand-in that records the CURRENT task's
    builtin-runtime state at the exact point a real tool call would read
    it — i.e. after activate_runtime_bindings() has (or hasn't) run."""

    async def _fake(*args: Any, **kwargs: Any) -> Any:
        try:
            bound_identity = builtin_runtime._identity_var.get()
            observed[key] = {
                "workspace": str(builtin_runtime.workspace()),
                "identity_did": bound_identity.did if bound_identity is not None else "",
            }
        except RuntimeError as exc:
            observed[key] = {"error": str(exc)}

        async def _gen() -> Any:
            yield TokenEvent(text="ok")
            yield TurnEndEvent(final_text="ok", tool_calls_made=0)

        return _gen()

    return _fake


class _PreSeededAgentFactory:
    """Mirrors bootstrap._make_agent_factory + arcui.embedded_agents's
    caching contract, but every entry is pre-seeded by the test (already
    started, in the test's own task) — the factory's __call__ is ALWAYS a
    cache hit here, exactly like turn 2+ for a real cached agent."""

    def __init__(self) -> None:
        self._cache: dict[str, ArcAgent] = {}

    def seed(self, agent: ArcAgent) -> str:
        identity = agent._identity
        assert identity is not None, "seed() requires an already-started agent"
        self._cache[identity.did] = agent
        return identity.did

    async def __call__(self, agent_did: str) -> ArcAgent:
        return self._cache[agent_did]


def _make_event(*, agent_did: str, session_key: str, message: str = "hi") -> InboundEvent:
    return InboundEvent(
        platform="test",
        chat_id=session_key,
        user_did="did:arc:user:tester",
        agent_did=agent_did,
        session_key=session_key,
        message=message,
    )


@pytest.fixture(autouse=True)
def _reset_builtin_runtime() -> None:
    builtin_runtime.reset()


class TestSameAgentTwoSiblingTurns:
    """(a) REGRESSION — the exact bug this file exists to close."""

    @patch("arcagent.core.model_manager.load_eval_model")
    async def test_second_turn_in_a_fresh_sibling_task_still_works(
        self,
        mock_load_model: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_load_model.return_value = MagicMock()
        workspace = tmp_path / "josh_agent" / "workspace"
        workspace.mkdir(parents=True)

        config = _agent_config("josh_agent", workspace, tmp_path / "josh_agent" / "keys")
        agent = await _started_agent(config)

        factory = _PreSeededAgentFactory()
        real_did = factory.seed(agent)

        executor = AsyncioExecutor(agent_factory=factory)
        router = SessionRouter(executor)

        observed: dict[str, _Observation] = {}
        with patch(
            "arcagent.core.agent_dispatch.arcrun_run_stream",
            side_effect=_fake_run_stream_recording(observed, "turn1"),
        ):
            await router.handle(_make_event(agent_did=real_did, session_key="s1", message="turn1"))
            await asyncio.sleep(0.05)

        with patch(
            "arcagent.core.agent_dispatch.arcrun_run_stream",
            side_effect=_fake_run_stream_recording(observed, "turn2"),
        ):
            # A SECOND, independent asyncio.Task — SessionRouter.handle()
            # spawns a fresh one per turn; this is NOT a child of turn 1's
            # task, which already completed.
            await router.handle(_make_event(agent_did=real_did, session_key="s2", message="turn2"))
            await asyncio.sleep(0.05)

        assert "error" not in observed["turn1"], f"turn 1 unexpectedly failed: {observed['turn1']}"
        assert "error" not in observed["turn2"], (
            f"turn 2 (fresh sibling task) must see the agent's bound state, not "
            f"raise 'not configured': {observed['turn2']}"
        )
        expected_workspace = str(workspace.resolve())
        assert (
            observed["turn1"]["workspace"] == observed["turn2"]["workspace"] == expected_workspace
        )
        assert observed["turn1"]["identity_did"] == observed["turn2"]["identity_did"] == real_did

        await agent.shutdown()


class TestTwoAgentsInterleaved:
    """(b) SECURITY — the b884f71 guarantee must survive the hotfix."""

    @patch("arcagent.core.model_manager.load_eval_model")
    async def test_interleaved_turns_never_cross_agent_state(
        self,
        mock_load_model: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_load_model.return_value = MagicMock()
        josh_ws = tmp_path / "josh_agent" / "workspace"
        coder_ws = tmp_path / "coder_agent" / "workspace"
        josh_ws.mkdir(parents=True)
        coder_ws.mkdir(parents=True)

        josh_agent = await _started_agent(
            _agent_config("josh_agent", josh_ws, tmp_path / "josh_agent" / "keys")
        )
        coder_agent = await _started_agent(
            _agent_config("coder_agent", coder_ws, tmp_path / "coder_agent" / "keys")
        )

        factory = _PreSeededAgentFactory()
        josh_did = factory.seed(josh_agent)
        coder_did = factory.seed(coder_agent)

        executor = AsyncioExecutor(agent_factory=factory)
        router = SessionRouter(executor)

        observed: dict[str, _Observation] = {}
        with patch(
            "arcagent.core.agent_dispatch.arcrun_run_stream",
            side_effect=_fake_run_stream_recording(observed, "josh_turn"),
        ):
            await router.handle(
                _make_event(agent_did=josh_did, session_key="josh-1", message="josh turn")
            )
            await asyncio.sleep(0.05)

        with patch(
            "arcagent.core.agent_dispatch.arcrun_run_stream",
            side_effect=_fake_run_stream_recording(observed, "coder_turn"),
        ):
            await router.handle(
                _make_event(agent_did=coder_did, session_key="coder-1", message="coder turn")
            )
            await asyncio.sleep(0.05)

        assert "error" not in observed["josh_turn"], observed["josh_turn"]
        assert "error" not in observed["coder_turn"], observed["coder_turn"]
        assert observed["josh_turn"]["workspace"] == str(josh_ws.resolve())
        assert observed["josh_turn"]["identity_did"] == josh_did
        assert observed["coder_turn"]["workspace"] == str(coder_ws.resolve())
        assert observed["coder_turn"]["identity_did"] == coder_did, (
            "coder's turn must sign/act with coder's OWN identity, never josh's — "
            "the exact live-incident mechanism (task 27) must stay closed"
        )

        await josh_agent.shutdown()
        await coder_agent.shutdown()
