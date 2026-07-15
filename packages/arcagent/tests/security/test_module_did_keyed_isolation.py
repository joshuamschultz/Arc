"""Cross-agent isolation for the DID-keyed module runtimes (memory + workpad).

Confirmed runtime bleed (SECURITY-CRITICAL, ASI03 / LLM02): a process runs many
agents concurrently (the embedded gateway caches up to 32). Both the memory and
workpad module runtimes held their per-agent state in a SINGLE process-global
slot, resolved with NO check that it belonged to the agent whose turn was
running. The agent that ``configure()``-d LAST won the slot; a different agent's
in-flight turn then read that agent's PRIVATE state — for memory, its Brain (its
private recall) injected straight into a foreign LLM prompt.

The fix has two independent guards, and these tests pin BOTH:

* **DID-keyed registry** — every agent's state lives under its own ``agent_did``,
  so a later ``configure`` can never clobber an earlier agent's slot.
* **Fail-closed resolution** — ``state()`` resolves by the DID bound for the
  RUNNING turn and refuses (raises + audits) on any missing binding, missing
  registration, or DID mismatch, instead of handing back another agent's state.

These operate on the module ``_runtime`` directly (no full ``ArcAgent``) so they
isolate the isolation invariant itself. The interleaving proof forces real
task interleaving (per feedback_concurrency_tests_must_interleave — sequential
calls alone don't prove a concurrency guarantee).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from arcagent.brain import NullBrain
from arcagent.core.config import EvalConfig
from arcagent.modules.memory import _runtime as memory_runtime
from arcagent.modules.memory.config import MemoryConfig
from arcagent.modules.workpad import _runtime as workpad_runtime
from arcagent.modules.workpad.config import WorkpadConfig


class _StubTelemetry:
    """Records audit events so the fail-closed audit emission is observable."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event: str, detail: dict[str, Any]) -> None:
        self.events.append((event, detail))

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]


class _TaggedBrain:
    """A sentinel Brain carrying a tag, so a resolved brain's owner is checkable."""

    def __init__(self, tag: str) -> None:
        self.tag = tag


def _memory_state(did: str, brain: Any, telemetry: Any = None) -> memory_runtime._State:
    return memory_runtime._State(
        config=MemoryConfig(),
        brain=brain,
        workspace=Path("."),
        telemetry=telemetry,
        bus=None,
        agent_did=did,
        active=not isinstance(brain, NullBrain),
    )


def _workpad_state(did: str, workspace: Path, telemetry: Any = None) -> workpad_runtime._State:
    return workpad_runtime._State(
        config=WorkpadConfig(),
        eval_config=EvalConfig(),
        workspace=workspace,
        telemetry=telemetry,
        llm_config=None,
        eval_label=f"{did}/workpad",
        agent_did=did,
        semaphore=asyncio.Semaphore(1),
    )


@pytest.fixture(autouse=True)
def _reset() -> Any:
    memory_runtime.reset()
    workpad_runtime.reset()
    yield
    memory_runtime.reset()
    workpad_runtime.reset()


_A = "did:arc:agent:alice"
_B = "did:arc:agent:bob"


class TestMemoryCrossAgentIsolation:
    """The memory Brain (private recall) must never resolve across agents."""

    def test_bound_did_resolves_own_brain_not_last_configured(self) -> None:
        """Both agents are configured in the process; Bob is the LAST writer (the
        old last-writer-wins winner). Alice's turn — rebinding Alice's DID as the
        current turn, exactly as ``activate_runtime_bindings`` does — must resolve
        Alice's own Brain, never Bob's."""
        alice_brain = _TaggedBrain("alice")
        bob_brain = _TaggedBrain("bob")
        memory_runtime.bind(_memory_state(_A, alice_brain))
        memory_runtime.bind(_memory_state(_B, bob_brain))  # Bob configured last

        # Alice's turn entry rebinds Alice's DID as the running turn's agent.
        memory_runtime.bind(_memory_state(_A, alice_brain))
        resolved: Any = memory_runtime.state().brain
        assert resolved is alice_brain, (
            "Alice's turn must resolve Alice's Brain, not the last-configured Bob's "
            "— the exact private-recall cross-agent bleed this fix closes"
        )

    @pytest.mark.asyncio
    async def test_interleaved_turns_never_cross_brains(self) -> None:
        """Two turns interleave on the event loop (Bob's ``configure`` runs while
        Alice's turn is suspended mid-flight). Each turn, in its own task, must
        read ITS OWN Brain — the current-DID binding is task-local."""
        alice_brain = _TaggedBrain("alice")
        bob_brain = _TaggedBrain("bob")

        bob_may_configure = asyncio.Event()
        alice_may_read = asyncio.Event()
        observed: dict[str, Any] = {}

        async def alice_turn() -> None:
            memory_runtime.bind(_memory_state(_A, alice_brain))
            bob_may_configure.set()
            await alice_may_read.wait()  # suspended mid-turn, as an LLM await would
            observed["alice"] = memory_runtime.state().brain

        async def bob_turn() -> None:
            await bob_may_configure.wait()
            memory_runtime.bind(_memory_state(_B, bob_brain))  # clobber attempt
            alice_may_read.set()

        await asyncio.gather(alice_turn(), bob_turn())
        assert observed["alice"] is alice_brain, (
            "Alice's suspended-then-resumed turn must still read Alice's Brain "
            "after Bob's concurrent configure() — never Bob's private recall"
        )

    def test_single_agent_resolves_normally(self) -> None:
        """The common single-agent path is unchanged: a configured agent resolves
        its own state."""
        alice_brain = _TaggedBrain("alice")
        memory_runtime.bind(_memory_state(_A, alice_brain))
        resolved: Any = memory_runtime.state().brain
        assert resolved is alice_brain

    def test_unbound_read_fails_closed_and_audits(self) -> None:
        """A fresh sibling task that never rebound (registry persists, but the
        task-local current-DID does not) must REFUSE the read, not fall back to
        the one registered agent."""
        telemetry = _StubTelemetry()
        memory_runtime.bind(_memory_state(_A, _TaggedBrain("alice"), telemetry))
        memory_runtime._current_did.set("")  # simulate the un-rebound sibling task

        with pytest.raises(memory_runtime.MemoryIsolationError, match="no agent DID bound"):
            memory_runtime.state()
        assert "memory.isolation_fault" in telemetry.event_names()

    def test_bound_to_unregistered_agent_fails_closed_and_audits(self) -> None:
        """A current DID pointing at an agent that was never registered must
        refuse — never fall back to whatever else is in the registry."""
        telemetry = _StubTelemetry()
        memory_runtime.bind(_memory_state(_A, _TaggedBrain("alice"), telemetry))
        memory_runtime._current_did.set("did:arc:agent:ghost")

        with pytest.raises(
            memory_runtime.MemoryIsolationError, match="no memory state registered"
        ):
            memory_runtime.state()
        assert "memory.isolation_fault" in telemetry.event_names()

    def test_did_mismatch_fails_closed_and_audits(self) -> None:
        """Defense in depth: a registry entry whose stored DID disagrees with its
        key (corruption) must be refused, never returned."""
        telemetry = _StubTelemetry()
        corrupt = _memory_state(_B, _TaggedBrain("bob"), telemetry)  # state says Bob
        memory_runtime._registry[_A] = corrupt  # but keyed under Alice
        memory_runtime._current_did.set(_A)

        with pytest.raises(memory_runtime.MemoryIsolationError, match="does not match"):
            memory_runtime.state()
        assert "memory.isolation_fault" in telemetry.event_names()


class TestWorkpadCrossAgentIsolation:
    """The workpad cockpit/transcript must never resolve across agents."""

    def test_bound_did_resolves_own_state_not_last_configured(self, tmp_path: Path) -> None:
        alice_ws = tmp_path / "alice"
        bob_ws = tmp_path / "bob"
        alice_ws.mkdir()
        bob_ws.mkdir()
        workpad_runtime.bind(_workpad_state(_A, alice_ws))
        workpad_runtime.bind(_workpad_state(_B, bob_ws))  # Bob configured last

        workpad_runtime.bind(_workpad_state(_A, alice_ws))
        st = workpad_runtime.state()
        assert st.agent_did == _A
        assert st.workspace == alice_ws, (
            "Alice's turn must resolve Alice's own cockpit workspace, not Bob's"
        )

    @pytest.mark.asyncio
    async def test_interleaved_turns_never_cross_state(self, tmp_path: Path) -> None:
        alice_ws = tmp_path / "alice"
        bob_ws = tmp_path / "bob"
        alice_ws.mkdir()
        bob_ws.mkdir()

        bob_may_configure = asyncio.Event()
        alice_may_read = asyncio.Event()
        observed: dict[str, Any] = {}

        async def alice_turn() -> None:
            workpad_runtime.bind(_workpad_state(_A, alice_ws))
            bob_may_configure.set()
            await alice_may_read.wait()
            observed["alice"] = workpad_runtime.state().workspace

        async def bob_turn() -> None:
            await bob_may_configure.wait()
            workpad_runtime.bind(_workpad_state(_B, bob_ws))
            alice_may_read.set()

        await asyncio.gather(alice_turn(), bob_turn())
        assert observed["alice"] == alice_ws, (
            "Alice's suspended-then-resumed turn must still read Alice's own "
            "cockpit workspace after Bob's concurrent configure()"
        )

    def test_single_agent_resolves_normally(self, tmp_path: Path) -> None:
        alice_ws = tmp_path / "alice"
        alice_ws.mkdir()
        workpad_runtime.bind(_workpad_state(_A, alice_ws))
        assert workpad_runtime.state().workspace == alice_ws

    def test_unbound_read_fails_closed_and_audits(self, tmp_path: Path) -> None:
        alice_ws = tmp_path / "alice"
        alice_ws.mkdir()
        telemetry = _StubTelemetry()
        workpad_runtime.bind(_workpad_state(_A, alice_ws, telemetry))
        workpad_runtime._current_did.set("")

        with pytest.raises(workpad_runtime.WorkpadIsolationError, match="no agent DID bound"):
            workpad_runtime.state()
        assert "workpad.isolation_fault" in telemetry.event_names()

    def test_bound_to_unregistered_agent_fails_closed_and_audits(self, tmp_path: Path) -> None:
        alice_ws = tmp_path / "alice"
        alice_ws.mkdir()
        telemetry = _StubTelemetry()
        workpad_runtime.bind(_workpad_state(_A, alice_ws, telemetry))
        workpad_runtime._current_did.set("did:arc:agent:ghost")

        with pytest.raises(
            workpad_runtime.WorkpadIsolationError, match="no workpad state registered"
        ):
            workpad_runtime.state()
        assert "workpad.isolation_fault" in telemetry.event_names()
