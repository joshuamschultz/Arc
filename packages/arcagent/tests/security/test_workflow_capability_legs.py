"""T-846 / SPEC-061 COMP-015 — accumulated legs survive a fresh node session.

The hole this closes: a workflow executes each node in its OWN session, and the
lethal-trifecta ledger accumulates per session. Without threading, a definition
whose first node reads private data and whose second node communicates
externally completes a forbidden composition that no single session could — the
graph launders the composition across a session boundary.

Every assertion here runs through the REAL arctrust ``PolicyPipeline`` built by
``core.tool_policy.build_pipeline``, the REAL ``ToolRegistry`` dispatch wrapper,
and the REAL ``SessionCapabilityLedger``. Nothing about the deny decision is
mocked — a stub would prove only that the test author knows what the answer
should be.

The forbidden composition under test is ``{private_data, external_comms}``,
which is a legitimate deployment policy and is what the two-node scenario in
T-846 actually spans. The three-leg trifecta is covered by
``test_trifecta_e2e.py``; what is new here is the SESSION BOUNDARY.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust.identity import AgentIdentity

from arcagent.core.config import ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal.capability_ledger import (
    EXTERNAL_COMMS,
    PRIVATE_DATA,
    CarriedLegs,
    SessionCapabilityLedger,
    bind_carried_legs,
    bind_session_id,
    reset_carried_legs,
    reset_session_id,
)
from arcagent.core.tool_policy import PolicyDenied, build_pipeline
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport

# The composition a two-node run spans: read private data, then talk outward.
_FORBIDDEN = frozenset({PRIVATE_DATA, EXTERNAL_COMMS})


class _Telemetry:
    def audit_event(self, event: str, payload: dict[str, Any]) -> None:
        del event, payload

    def tool_span(self, *_a: Any, **_k: Any) -> Any:
        class _Span:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_e: Any) -> None:
                return None

        return _Span()


def _tool(name: str, tags: list[str], workspace: Path) -> RegisteredTool:
    async def execute(**_kw: Any) -> str:
        return f"{name}-ok"

    del workspace
    return RegisteredTool(
        name=name,
        description=name,
        input_schema={},
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="test",
        classification="state_modifying",
        capability_tags=tags,
    )


def _registry(workspace: Path) -> tuple[ToolRegistry, SessionCapabilityLedger]:
    """A REAL registry over a REAL policy pipeline — no gate, so a deny stands."""
    identity = AgentIdentity.generate("org", "agent")
    ledger = SessionCapabilityLedger()
    pipeline = build_pipeline(
        tier="personal",
        agent_registry={identity.did: identity.public_key},
        forbidden_compositions=[_FORBIDDEN],
    )
    registry = ToolRegistry(
        config=ToolsConfig(),
        bus=ModuleBus(),
        telemetry=_Telemetry(),
        policy_pipeline=pipeline,
        identity=identity,
        tier="personal",
        capability_ledger=ledger,
        # No human gate: a denied composition must stay denied so the test reads
        # the policy decision itself rather than an approval outcome.
        human_gate=None,
    )
    registry.register(_tool("read_customer_record", ["file_read"], workspace))
    registry.register(_tool("send_summary", ["network_egress"], workspace))
    return registry, ledger


async def _run_node(
    registry: ToolRegistry, tool_name: str, session_id: str, carrier: CarriedLegs | None
) -> Any:
    """Execute one node: a fresh session, optionally carrying the run's legs."""
    legs_token = bind_carried_legs(carrier) if carrier is not None else None
    session_token = bind_session_id(session_id)
    try:
        wrapped = registry._create_wrapped_execute(registry.tools[tool_name])
        return await wrapped({})
    finally:
        reset_session_id(session_token)
        if legs_token is not None:
            reset_carried_legs(legs_token)


@pytest.mark.asyncio
class TestLegsCrossTheNodeSessionBoundary:
    async def test_second_node_is_denied_through_the_real_policy_pipeline(
        self, tmp_path: Path
    ) -> None:
        """Node 1 reads private data; node 2's egress is DENIED in a fresh session."""
        registry, _ledger = _registry(tmp_path)
        carrier = CarriedLegs()

        # Node 1 — its own session. Allowed; lights private_data.
        assert await _run_node(registry, "read_customer_record", "node-1", carrier) == (
            "read_customer_record-ok"
        )
        assert carrier.snapshot() == frozenset({PRIVATE_DATA})

        # Node 2 — a genuinely FRESH session id, carrying the run's legs.
        with pytest.raises(PolicyDenied):
            await _run_node(registry, "send_summary", "node-2", carrier)

    async def test_without_threading_the_same_second_node_is_allowed(
        self, tmp_path: Path
    ) -> None:
        """The control: this is exactly the hole, so the test above cannot pass vacuously.

        If leg threading were dead wiring, the assertion above would still fail
        to catch it unless the un-threaded case demonstrably succeeds. It does:
        a fresh session with no carrier sees an empty union and the egress is
        allowed — which is the exfiltration path.
        """
        registry, _ledger = _registry(tmp_path)

        await _run_node(registry, "read_customer_record", "node-1", None)
        assert await _run_node(registry, "send_summary", "node-2", None) == "send_summary-ok"

    async def test_first_node_alone_is_never_denied(self, tmp_path: Path) -> None:
        """Threading must not deny a run that has not actually composed anything."""
        registry, _ledger = _registry(tmp_path)
        carrier = CarriedLegs()
        assert await _run_node(registry, "send_summary", "node-1", carrier) == "send_summary-ok"

    async def test_legs_lit_inside_a_node_are_carried_back_out(self, tmp_path: Path) -> None:
        """The carrier is bidirectional: what a node lights, the run inherits."""
        registry, _ledger = _registry(tmp_path)
        carrier = CarriedLegs()
        await _run_node(registry, "read_customer_record", "node-1", carrier)
        assert PRIVATE_DATA in carrier.snapshot()


class TestAccumulationIsBounded:
    """SPEC-009 lesson — a per-run security collection must have a ceiling."""

    def test_absorb_respects_the_bound_deterministically(self) -> None:
        carrier = CarriedLegs(max_legs=3)
        carrier.absorb(frozenset({"e", "d", "c", "b", "a"}))
        assert carrier.snapshot() == frozenset({"a", "b", "c"})

    def test_repeated_absorb_never_grows_past_the_bound(self) -> None:
        carrier = CarriedLegs(max_legs=2)
        for leg in "abcdefgh":
            carrier.absorb(frozenset({leg}))
        assert len(carrier.snapshot()) == 2

    def test_snapshot_is_an_immutable_copy(self) -> None:
        carrier = CarriedLegs(legs={"a"})
        snapshot = carrier.snapshot()
        carrier.absorb(frozenset({"b"}))
        assert snapshot == frozenset({"a"})


@pytest.mark.asyncio
class TestLedgerSnapshotUnionsTheCarrier:
    """The union happens in the ledger, so EVERY policy read sees it."""

    async def test_snapshot_includes_carried_legs(self) -> None:
        ledger = SessionCapabilityLedger()
        carrier = CarriedLegs(legs={PRIVATE_DATA})
        token = bind_carried_legs(carrier)
        try:
            assert ledger.snapshot("fresh-session") == frozenset({PRIVATE_DATA})
        finally:
            reset_carried_legs(token)

    async def test_session_bucket_stays_the_honest_record_of_what_it_lit(self) -> None:
        ledger = SessionCapabilityLedger()
        carrier = CarriedLegs(legs={PRIVATE_DATA})
        token = bind_carried_legs(carrier)
        try:
            ledger.record("s", frozenset({EXTERNAL_COMMS}))
        finally:
            reset_carried_legs(token)
        # Outside the carrier binding the session shows only its OWN legs.
        assert ledger.snapshot("s") == frozenset({EXTERNAL_COMMS})
        assert carrier.snapshot() == frozenset({PRIVATE_DATA, EXTERNAL_COMMS})
