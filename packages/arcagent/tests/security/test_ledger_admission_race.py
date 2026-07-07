"""SPEC-043 REQ-032 — INTERLEAVING-FORCED ledger admission race (T-001/T-D3).

Two tool calls, each individually allowed, whose capability-leg UNION completes
the lethal trifecta, are dispatched concurrently. An ``asyncio.Barrier(2)`` forces
BOTH into ``pipeline.evaluate`` before either records — the exact TOCTOU window in
``wrapped_execute`` (``snapshot → await evaluate → record``).

Per ``feedback_concurrency_tests_must_interleave``: an instant mock makes
``asyncio.gather`` run tasks sequentially and the race never manifests. The
barrier (with a timeout so the *guarded* path does not deadlock when the lock
serializes the two tasks) forces the interleave.

Expected: exactly ONE of the two completing calls is DENIED (the union is seen).
This FAILS on the unguarded ledger (both allowed → lost update) and PASSES once
``SessionCapabilityLedger.admission_lock`` serializes snapshot→evaluate→record.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from arctrust.identity import AgentIdentity
from nacl.signing import SigningKey  # noqa: F401  (kept parity with sibling fixtures)

from arcagent.core.config import ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal.capability_ledger import (
    LETHAL_TRIFECTA,
    SessionCapabilityLedger,
    bind_session_id,
    reset_session_id,
)
from arcagent.core.tool_policy import PolicyDenied, build_pipeline
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport


class _Telemetry:
    def audit_event(self, event: str, payload: dict) -> None: ...

    def tool_span(self, *_a: Any, **_k: Any) -> Any:
        class _Span:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_e: Any) -> None:
                return None

        return _Span()


def _tool(name: str, tags: list[str]) -> RegisteredTool:
    async def execute(**_kwargs: Any) -> str:
        return f"{name}-ok"

    return RegisteredTool(
        name=name,
        description=name,
        input_schema={},
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="test",
        classification="read_only",
        capability_tags=tags,
    )


def _registry(identity: AgentIdentity) -> ToolRegistry:
    pipeline = build_pipeline(
        tier="personal",
        agent_registry={identity.did: identity.public_key},
        forbidden_compositions=[LETHAL_TRIFECTA],
    )
    reg = ToolRegistry(
        config=ToolsConfig(),
        bus=ModuleBus(),
        telemetry=_Telemetry(),
        policy_pipeline=pipeline,
        identity=identity,
        tier="personal",
        capability_ledger=SessionCapabilityLedger(),
        human_gate=None,  # no gate → a trifecta-completing deny fails closed
    )
    reg.register(_tool("reader", ["file_read"]))  # private_data
    reg.register(_tool("fetch", ["extract"]))  # untrusted_input
    reg.register(_tool("egress", ["network_egress"]))  # external_comms
    return reg


async def _dispatch(reg: ToolRegistry, name: str) -> Any:
    wrapped = reg._create_wrapped_execute(reg.tools[name])
    return await wrapped({})


def _install_barrier(reg: ToolRegistry, barrier: asyncio.Barrier) -> None:
    """Force both concurrent dispatches to sit inside evaluate simultaneously."""
    pipeline = reg._policy_pipeline
    real_eval = pipeline.evaluate  # type: ignore[union-attr]

    async def slow_eval(call: Any, ctx: Any) -> Any:
        try:
            await asyncio.wait_for(barrier.wait(), timeout=0.5)
        except (TimeoutError, asyncio.BrokenBarrierError):
            pass
        return await real_eval(call, ctx)

    pipeline.evaluate = slow_eval  # type: ignore[union-attr,assignment]


@pytest.mark.asyncio
async def test_concurrent_union_not_both_allowed() -> None:
    identity = AgentIdentity.generate("org", "agent")
    reg = _registry(identity)

    token = bind_session_id("race")
    try:
        # Seed one leg (private_data). Now fetch (+untrusted) and egress
        # (+external) are each individually allowed, but their union with the
        # seed completes the trifecta.
        assert await _dispatch(reg, "reader") == "reader-ok"

        _install_barrier(reg, asyncio.Barrier(2))
        results = await asyncio.gather(
            _dispatch(reg, "fetch"),
            _dispatch(reg, "egress"),
            return_exceptions=True,
        )
    finally:
        reset_session_id(token)

    denials = [r for r in results if isinstance(r, PolicyDenied)]
    # Exactly one completing call must be denied — they are NOT both allowed.
    assert len(denials) == 1, f"expected 1 denial, got {len(denials)} — ledger race (lost update)"


@pytest.mark.asyncio
async def test_admission_lock_does_not_over_serialize_execution() -> None:
    """REQ-032 regression: the lock covers only the O(1) admission decision.

    A slow tool.execute in one branch must NOT block another branch's execution
    — proven by overlapping execution windows. Guards against over-locking.
    """
    identity = AgentIdentity.generate("org", "agent")
    reg = _registry(identity)

    started = asyncio.Event()
    overlap = {"value": False}

    async def slow_exec(**_kwargs: Any) -> str:
        if started.is_set():
            overlap["value"] = True  # a sibling was already executing
        started.set()
        await asyncio.sleep(0.05)
        return "ok"

    for name in ("a", "b"):
        tool = _tool(name, [])  # no trifecta legs → both always allowed
        tool.execute = slow_exec  # type: ignore[assignment]
        reg.register(tool)

    token = bind_session_id("nolock")
    try:
        await asyncio.gather(_dispatch(reg, "a"), _dispatch(reg, "b"))
    finally:
        reset_session_id(token)

    assert overlap["value"] is True, "executions did not overlap — lock held across execute"
