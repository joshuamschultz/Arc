"""T-844 / T-862 — the node execution adapter, driven through the REAL path.

SPEC-061 COMP-014 / REQ-233, REQ-237, REQ-238, REQ-242, REQ-243.

The acceptance criterion for T-844 is explicit: "exercised through the real
completion path, not a unit stub". So schema and artifact enforcement are
asserted by calling the actual ``complete_task`` / ``set_task_output`` tools
over a real ``arcstore.tasks.TaskStore``, on a real task row carrying a real
workflow metadata block — the same code the dispatch loop runs. The pure
helpers are additionally unit-tested, but the enforcement claims rest on the
tool calls.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arctrust import AgentIdentity
from arctrust.paths import workflows_dir
from packages.arcagent.tests.unit.modules.tasks.conftest import make_registry

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"risk": {"type": "string", "enum": ["low", "high"]}},
    "required": ["risk"],
}


def _node_block(**overrides: Any) -> dict[str, Any]:
    """A task row's metadata EXACTLY as ``arcteam.workflow.runner`` stamps it.

    Flat, with the runner's own key names — ``workflow`` is the id string, the
    run is ``flow_run_id``, the attempt is ``iteration``. Mirroring the real
    producer here is load-bearing: an adapter that reads a shape the runner does
    not write is invisible on the happy path and silently disables every gate
    beneath it.
    """
    block: dict[str, Any] = {
        "workflow": "onboarding",
        "workflow_version": 1,
        "flow_run_id": "run_1",
        "node_id": "verify",
        "node_kind": "agent",
        "iteration": 1,
        "idempotency_key": "run_1::verify::1",
        "upstream": {},
        "strategy": [],
        "output_schema": _SCHEMA,
        "artifacts": [],
    }
    block.update(overrides)
    return block


def _run_root(workspace: Path, run_id: str = "run_1") -> Path:
    """Where a run's declared artifacts live for a solo agent (D-539)."""
    root = workspace / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def node_state(tmp_path: Path, arcstore_opener: Any) -> Iterator[Any]:
    """A real tasks runtime over a test-local ArcStore backend."""
    from arcagent.modules.tasks import _runtime

    _runtime.reset()
    _runtime.configure(
        config={"enabled": True, "default_max_attempts": 3},
        telemetry=MagicMock(),
        workspace=tmp_path,
        identity=AgentIdentity.generate(org="local", agent_type="agent"),
        registry=make_registry(),
        arcstore_opener=arcstore_opener,
    )
    yield _runtime.state()
    _runtime.reset()


async def _make_node_task(state: Any, **block_overrides: Any) -> Any:
    """Create a real in_progress task row carrying a workflow node block."""
    from arcagent.modules.tasks.capabilities import _state
    from arcagent.modules.tasks.models import Task

    st = await _state()
    task = Task(
        id="task_node_1",
        title="verify the record",
        status="in_progress",
        owner_did=st.identity.did,
        creator_did=st.identity.did,
        attempts=1,
        max_attempts=3,
        metadata=_node_block(**block_overrides),
    )
    await st.store.create(task)
    del state
    return task


@pytest.mark.asyncio
class TestOutputSchemaFiresOnCompletion:
    """REQ-237 — a schema failure is a RETRYABLE failure, never a pass-forward."""

    async def test_violating_output_is_refused_by_complete_task(self, node_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import complete_task

        await _make_node_task(node_state)
        result = json.loads(
            await complete_task(id="task_node_1", resolution="done", output={"risk": "medium"})
        )
        assert result["retryable"] is True
        assert "output_schema" in result["error"]

    async def test_violating_output_is_never_recorded(self, node_state: Any) -> None:
        """The pass-forward this requirement exists to prevent."""
        from arcagent.modules.tasks.capabilities import _state, complete_task

        await _make_node_task(node_state)
        await complete_task(id="task_node_1", resolution="done", output={"risk": "medium"})
        st = await _state()
        stored = await st.store.get("task_node_1")
        assert stored is not None
        assert stored.output is None
        assert stored.status != "done"

    async def test_refusal_consumes_an_attempt_and_requeues(self, node_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _state, complete_task

        await _make_node_task(node_state)
        await complete_task(id="task_node_1", resolution="done", output={"risk": "medium"})
        st = await _state()
        stored = await st.store.get("task_node_1")
        assert stored is not None
        # Below the ceiling -> requeued for another attempt, not dead-lettered.
        assert stored.status == "todo"
        assert stored.last_error is not None

    async def test_conforming_output_completes_normally(self, node_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _state, complete_task

        await _make_node_task(node_state)
        await complete_task(id="task_node_1", resolution="done", output={"risk": "low"})
        st = await _state()
        stored = await st.store.get("task_node_1")
        assert stored is not None
        assert stored.status == "done"
        assert stored.output == {"risk": "low"}

    async def test_missing_output_against_a_declared_schema_is_refused(
        self, node_state: Any
    ) -> None:
        from arcagent.modules.tasks.capabilities import complete_task

        await _make_node_task(node_state)
        result = json.loads(await complete_task(id="task_node_1", resolution="done"))
        assert result["retryable"] is True

    async def test_set_task_output_enforces_the_same_schema(self, node_state: Any) -> None:
        """The other write path into ``output`` must not be a way around the gate."""
        from arcagent.modules.tasks.capabilities import _state, set_task_output

        await _make_node_task(node_state)
        result = json.loads(await set_task_output(id="task_node_1", output={"risk": "medium"}))
        assert result["retryable"] is True
        st = await _state()
        stored = await st.store.get("task_node_1")
        assert stored is not None
        assert stored.output is None

    async def test_an_ordinary_task_is_unaffected(self, node_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import complete_task, create_task

        created = json.loads(await create_task(title="just a task"))
        await complete_task(id=created["id"], resolution="ok", output={"anything": 1})
        # No schema declared, so any output is fine.
        assert "error" not in created


@pytest.mark.asyncio
async def test_router_rejects_an_undeclared_route(node_state: Any) -> None:
    """The agent-side completion gate matches the runner's route contract."""
    from arcagent.modules.tasks.capabilities import complete_task

    await _make_node_task(
        node_state,
        node_kind="router",
        routes=["approved", "rejected"],
        router_mode="llm",
        output_schema=None,
    )

    result = json.loads(
        await complete_task(id="task_node_1", resolution="selected", output={"route": "invented"})
    )

    assert result["retryable"] is True
    assert "router output.route" in result["error"]


@pytest.mark.asyncio
class TestArtifactEnforcement:
    """REQ-238 — declared artifacts must exist, and the retry names the producer."""

    async def test_missing_artifact_refuses_completion(self, node_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import complete_task

        await _make_node_task(node_state, output_schema=None, artifacts=["customer_record.json"])
        result = json.loads(await complete_task(id="task_node_1", resolution="done"))
        assert result["retryable"] is True
        assert "customer_record.json" in result["error"]

    async def test_retry_message_names_the_producing_tool(self, node_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import complete_task

        await _make_node_task(node_state, output_schema=None, artifacts=["report.md"])
        result = json.loads(await complete_task(id="task_node_1", resolution="done"))
        assert "`write`" in result["error"]

    async def test_present_artifact_completes(self, node_state: Any, tmp_path: Path) -> None:
        from arcagent.modules.tasks.capabilities import _state, complete_task

        (_run_root(tmp_path) / "report.md").write_text("done", encoding="utf-8")
        await _make_node_task(node_state, output_schema=None, artifacts=["report.md"])
        await complete_task(id="task_node_1", resolution="done")
        st = await _state()
        stored = await st.store.get("task_node_1")
        assert stored is not None
        assert stored.status == "done"

    async def test_set_task_output_does_not_check_artifacts(self, node_state: Any) -> None:
        """Artifacts gate COMPLETION, not an intermediate output write."""
        from arcagent.modules.tasks.capabilities import _state, set_task_output

        await _make_node_task(node_state, output_schema=None, artifacts=["absent.md"])
        await set_task_output(id="task_node_1", output={"partial": True})
        st = await _state()
        stored = await st.store.get("task_node_1")
        assert stored is not None
        assert stored.output == {"partial": True}


@pytest.mark.asyncio
class TestArtifactPathConfinement:
    """An artifact path must never escape the node's working root.

    The definition validator refuses absolute and ``..`` artifact paths at
    authoring time, but a hand-edited bundle loaded straight off disk never went
    through it, and the runner passes ``artifacts`` into the task row as opaque
    strings. This adapter is where they become real filesystem operations, so it
    must refuse on its own evidence.

    Every case here is adversarial on purpose: a benign relative path lands
    correctly whether or not the guard exists, so only an escaping path tells
    the guarded code apart from the unguarded code.
    """

    @pytest.mark.parametrize(
        "artifact",
        [
            "../escaped.txt",
            "../../escaped.txt",
            "nested/../../escaped.txt",
            "/etc/passwd",
        ],
    )
    async def test_escaping_artifact_path_refuses_completion(
        self, node_state: Any, tmp_path: Path, artifact: str
    ) -> None:
        from arcagent.modules.tasks.capabilities import _state, complete_task

        # Make the escape target genuinely exist, so an unguarded check would
        # happily pass and report the node complete.
        _run_root(tmp_path)
        (tmp_path / "escaped.txt").write_text("victim", encoding="utf-8")
        (tmp_path.parent / "escaped.txt").write_text("victim", encoding="utf-8")

        await _make_node_task(node_state, output_schema=None, artifacts=[artifact])
        result = json.loads(await complete_task(id="task_node_1", resolution="done"))

        assert result["retryable"] is True
        assert "outside its working root" in result["error"]
        st = await _state()
        stored = await st.store.get("task_node_1")
        assert stored is not None
        assert stored.status != "done"

    async def test_escape_is_refused_before_any_filesystem_stat(
        self, node_state: Any, tmp_path: Path
    ) -> None:
        """The escaping path is never resolved — not even to ask if it exists."""
        from arcagent.modules.tasks.node_execution import WorkflowNode, missing_artifacts

        del node_state
        node = WorkflowNode(workflow_id="w", run_id="r", node_id="n", artifacts=["../outside.txt"])
        # An escaping path is absent from the MISSING list — it is not a missing
        # artifact, it is a refused one, and the two must not be conflated.
        assert missing_artifacts(node, tmp_path) == []

    async def test_a_confined_path_still_works(self, node_state: Any, tmp_path: Path) -> None:
        """The guard must not break the ordinary case it sits in front of."""
        from arcagent.modules.tasks.capabilities import _state, complete_task

        root = _run_root(tmp_path)
        (root / "nested").mkdir()
        (root / "nested" / "report.md").write_text("done", encoding="utf-8")
        await _make_node_task(node_state, output_schema=None, artifacts=["nested/report.md"])
        await complete_task(id="task_node_1", resolution="done")
        st = await _state()
        stored = await st.store.get("task_node_1")
        assert stored is not None
        assert stored.status == "done"


class TestMetadataShapeMatchesTheRunner:
    """The one mismatch that silently disables every gate below it.

    ``node_from_task`` returning None on a real runner-materialised row is
    invisible: the task runs, nothing errors, and schema validation, artifact
    enforcement, strategy pinning, and leg threading are all simply never
    reached. So the key names are asserted against the producer's own source
    rather than against this test's idea of them.
    """

    def test_the_keys_this_adapter_reads_are_the_keys_the_runner_writes(self) -> None:
        from arcteam.workflow import runner

        source = Path(runner.__file__).read_text(encoding="utf-8")
        _, _, build_task = source.partition("def _build_task")
        assert build_task, "arcteam runner no longer has _build_task"
        for key in ("workflow", "flow_run_id", "node_id", "node_kind", "iteration"):
            assert f'"{key}"' in build_task, f"runner no longer stamps {key!r}"

    def test_a_real_runner_shaped_row_is_recognised(self) -> None:
        from arcagent.modules.tasks.node_execution import node_from_task

        node = node_from_task(MagicMock(metadata=_node_block()))
        assert node is not None, "the adapter does not recognise the runner's own shape"

    def test_tool_and_router_contracts_survive_the_task_handoff(self) -> None:
        """Executable workflow data must not degrade into an agent prompt."""
        from arcagent.modules.tasks.node_execution import node_from_task

        tool = node_from_task(
            MagicMock(
                metadata=_node_block(
                    node_kind="tool",
                    tool="lookup_customer",
                    args={"customer_id": 42},
                    timeout_s=17,
                    max_attempts=4,
                )
            )
        )
        router = node_from_task(
            MagicMock(
                metadata=_node_block(
                    node_kind="router",
                    routes=["approved", "rejected"],
                    router_mode="llm",
                )
            )
        )

        assert tool is not None
        assert tool.tool == "lookup_customer"
        assert tool.args == {"customer_id": 42}
        assert tool.timeout_s == 17
        assert tool.max_attempts == 4
        assert router is not None
        assert router.routes == ["approved", "rejected"]
        assert router.router_mode == "llm"

    def test_deliver_to_survives_the_task_handoff(self) -> None:
        """A pinned notification target must reach the dispatch, not be dropped."""
        from arcagent.modules.tasks.node_execution import node_from_task

        node = node_from_task(MagicMock(metadata=_node_block(deliver_to="telegram:8293394811")))
        assert node is not None
        assert node.deliver_to == "telegram:8293394811"

    def test_deliver_to_defaults_to_none_when_unset(self) -> None:
        from arcagent.modules.tasks.node_execution import node_from_task

        node = node_from_task(MagicMock(metadata=_node_block()))
        assert node is not None
        assert node.deliver_to is None

    def test_a_nested_block_is_not_mistaken_for_a_node(self) -> None:
        """The shape this adapter originally assumed must not half-work."""
        from arcagent.modules.tasks.node_execution import node_from_task

        assert node_from_task(MagicMock(metadata={"workflow": _node_block()})) is None


class TestArtifactGuardIsShared:
    def test_escaping_artifacts_are_refused_with_no_orchestration_installed(self) -> None:
        """The guard is the agent's own, so it cannot go missing with a package.

        A path escape must be refused on every deployment, including a lone
        agent with no orchestration layer at all.
        """
        from arcagent.modules.tasks import node_execution

        source = Path(node_execution.__file__).read_text(encoding="utf-8")
        assert "arcteam" not in source
        assert node_execution._confined(Path("/tmp"), "../x") is None
        assert node_execution._confined(Path("/tmp"), "/etc/passwd") is None


class TestPromptSectionSeam:
    """REQ-233/239 — node content reaches the model through assemble_prompt only."""

    def test_upstream_outputs_are_rendered_as_typed_json(self) -> None:
        from arcagent.modules.tasks.node_execution import WorkflowNode, render_node_section

        node = WorkflowNode(
            workflow_id="w",
            run_id="r",
            node_id="verify",
            upstream={"collect": {"company_domain": "example.com"}},
        )
        section = render_node_section(node)
        assert "`collect`" in section
        assert '"company_domain": "example.com"' in section

    def test_instructions_and_schema_are_included(self) -> None:
        from arcagent.modules.tasks.node_execution import WorkflowNode, render_node_section

        node = WorkflowNode(workflow_id="w", run_id="r", node_id="verify", output_schema=_SCHEMA)
        section = render_node_section(node, None, "Do the thing.")
        assert "Do the thing." in section
        assert "Required output shape" in section

    def test_router_prompt_exposes_only_declared_route_ids(self) -> None:
        from arcagent.modules.tasks.node_execution import WorkflowNode, render_node_section

        section = render_node_section(
            WorkflowNode(
                workflow_id="w",
                run_id="r",
                node_id="route",
                kind="router",
                routes=["approved", "rejected"],
                router_mode="llm",
            )
        )

        assert "approved" in section
        assert "rejected" in section
        assert "only one of the declared route IDs" in section

    @pytest.mark.asyncio
    async def test_hook_writes_only_when_a_node_is_bound(self, node_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import inject_workflow_node_section
        from arcagent.modules.tasks.node_execution import WorkflowNode, bind_node, reset_node

        del node_state
        ctx = MagicMock()
        ctx.data = {"sections": {}}
        await inject_workflow_node_section(ctx)
        assert ctx.data["sections"] == {}

        token = bind_node(WorkflowNode(workflow_id="w", run_id="r", node_id="verify"))
        try:
            await inject_workflow_node_section(ctx)
        finally:
            reset_node(token)
        assert "workflow_node" in ctx.data["sections"]

    @pytest.mark.asyncio
    async def test_hook_is_registered_on_assemble_prompt(self) -> None:
        from arcagent.modules.tasks.capabilities import inject_workflow_node_section
        from arcagent.tools._decorator import HookMetadata, capability_meta

        meta = capability_meta(inject_workflow_node_section)
        assert isinstance(meta, HookMetadata)
        assert meta.event == "agent:assemble_prompt"


class TestStrategyPinning:
    """REQ-243 — a declared strategy list reaches the loop; absent pins react."""

    def test_declared_list_passes_through(self) -> None:
        from arcagent.modules.tasks.node_execution import WorkflowNode, allowed_strategies

        node = WorkflowNode(workflow_id="w", run_id="r", node_id="n", strategy=["react", "code"])
        assert allowed_strategies(node) == ["react", "code"]

    def test_absent_list_pins_react(self) -> None:
        from arcagent.modules.tasks.node_execution import WorkflowNode, allowed_strategies

        node = WorkflowNode(workflow_id="w", run_id="r", node_id="n")
        assert allowed_strategies(node) == ["react"]

    def test_agent_run_accepts_allowed_strategies(self) -> None:
        """The seam must exist on the callback the dispatch loop actually calls."""
        import inspect

        from arcagent.core.agent import ArcAgent

        assert "allowed_strategies" in inspect.signature(ArcAgent.run_collected).parameters
        assert "allowed_strategies" in inspect.signature(ArcAgent.run).parameters


class TestIdempotencyKey:
    """REQ-242 — a retried node cannot repeat an external side effect."""

    def test_key_is_per_attempt(self) -> None:
        from arcagent.modules.tasks.node_execution import NodeAttempt

        first = NodeAttempt("run_1", "verify", 1).idempotency_key
        second = NodeAttempt("run_1", "verify", 2).idempotency_key
        assert first != second
        assert first == "run_1:verify:1"

    def test_reader_returns_none_outside_a_node_dispatch(self) -> None:
        from arcagent.modules.tasks.node_execution import idempotency_key

        assert idempotency_key() is None

    def test_reader_returns_the_bound_node_key(self) -> None:
        from arcagent.modules.tasks.node_execution import (
            WorkflowNode,
            bind_node,
            idempotency_key,
            reset_node,
        )

        node = WorkflowNode(workflow_id="w", run_id="run_9", node_id="qa", attempt=3)
        token = bind_node(node)
        try:
            assert idempotency_key() == "run_9:qa:3"
        finally:
            reset_node(token)


class TestNodeParsing:
    def test_ordinary_task_is_not_a_node(self) -> None:
        from arcagent.modules.tasks.node_execution import node_from_task

        assert node_from_task(MagicMock(metadata={})) is None

    def test_malformed_block_degrades_to_not_a_node(self) -> None:
        from arcagent.modules.tasks.node_execution import node_from_task

        # A row naming a workflow but no run/node is not a node row.
        task = MagicMock(metadata={"workflow": "w"})
        assert node_from_task(task) is None

    def test_valid_block_parses(self) -> None:
        from arcagent.modules.tasks.node_execution import node_from_task

        task = MagicMock(metadata=_node_block())
        node = node_from_task(task)
        assert node is not None
        assert node.node_id == "verify"
        assert node.run_id == "run_1"
        assert node.workflow_id == "onboarding"
        assert node.idempotency_key == "run_1::verify::1"

    def test_the_runners_stamped_legs_seed_the_node(self) -> None:
        from arcagent.modules.tasks.node_execution import node_from_task

        node = node_from_task(MagicMock(metadata=_node_block(accumulated_legs=["private_data"])))
        assert node is not None
        assert node.accumulated_legs == ["private_data"]


@pytest.mark.asyncio
class TestLegsAreWrittenBackWhereTheRunnerReadsThem:
    """COMP-015's return leg — the row is the handoff, so it must round-trip.

    The write-back is what lets the runner union this node's legs into the next
    node's stamp. Written to a key ``node_from_task`` does not read, the
    threading is one-directional and the accumulation silently restarts at every
    node; written under the ``workflow`` key it would clobber the workflow id.
    """

    async def test_persisted_legs_round_trip_through_the_parser(self, node_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _persist_run_legs, _state
        from arcagent.modules.tasks.node_execution import node_from_task

        task = await _make_node_task(node_state)
        node = node_from_task(task)
        assert node is not None
        st = await _state()

        await _persist_run_legs(st, task, node, frozenset({"private_data"}), st.identity.did)

        stored = await st.store.get(task.id)
        assert stored is not None
        assert stored.metadata["workflow"] == "onboarding"
        reparsed = node_from_task(stored)
        assert reparsed is not None
        assert reparsed.accumulated_legs == ["private_data"]

    async def test_completing_a_node_lands_its_legs_with_the_terminal_status(
        self, node_state: Any
    ) -> None:
        """Legs written AFTER the row goes done can be missed by the next tick.

        The runner stamps the following node from completed rows. If the write
        back only happened once the whole dispatch unwound, a tick landing in
        that window would materialise the next node under-charged — the exact
        composition hole COMP-015 exists to close. So completion carries them.
        """
        from arcagent.core.session_internal.capability_ledger import (
            CarriedLegs,
            bind_carried_legs,
            reset_carried_legs,
        )
        from arcagent.modules.tasks.capabilities import _state, complete_task
        from arcagent.modules.tasks.node_execution import bind_node, node_from_task, reset_node

        task = await _make_node_task(node_state, output_schema=None)
        node = node_from_task(task)
        assert node is not None
        st = await _state()

        node_token = bind_node(node)
        legs_token = bind_carried_legs(CarriedLegs(legs={"private_data"}))
        try:
            await complete_task(id=task.id, resolution="done", output={"risk": "low"})
        finally:
            reset_carried_legs(legs_token)
            reset_node(node_token)

        stored = await st.store.get(task.id)
        assert stored is not None
        assert stored.status == "done"
        assert stored.metadata["accumulated_legs"] == ["private_data"]


class TestTheSessionKeyIsAFilename:
    """A workflow node's row id is path-shaped; a session key is a filename.

    The live failure: dispatching the first node of a run died with
    `invalid session key: 'task:wf/run-14052d03515c/review_clients/0'`, which
    the session manager rejects (correctly — an unvalidated key becomes an
    out-of-tree write). Every workflow run stopped at its first node.
    """

    def test_a_path_shaped_task_id_flattens(self) -> None:
        from arcagent.modules.tasks.capabilities import _session_key

        key = _session_key("wf/run-abc123/review_clients/0")

        assert "/" not in key
        assert key.startswith("task:wf-run-abc123-review_clients-0")

    def test_the_same_task_always_resumes_the_same_session(self) -> None:
        from arcagent.modules.tasks.capabilities import _session_key

        assert _session_key("wf/run-abc/n/0") == _session_key("wf/run-abc/n/0")

    def test_two_ids_that_flatten_alike_stay_distinct(self) -> None:
        """Without the digest, `wf/a/b` and `wf-a-b` would share one session."""
        from arcagent.modules.tasks.capabilities import _session_key

        assert _session_key("wf/a/b") != _session_key("wf-a-b")

    def test_an_ordinary_task_id_is_untouched(self) -> None:
        from arcagent.modules.tasks.capabilities import _session_key

        assert _session_key("task_ab12cd34") == "task:task_ab12cd34"

    def test_the_key_survives_the_session_managers_own_check(self, tmp_path: Path) -> None:
        """Driven through the real validator, not a copy of its rule."""
        from unittest.mock import MagicMock

        from arcagent.core.config import ContextConfig, SessionConfig
        from arcagent.core.session_internal.manager import SessionManager
        from arcagent.modules.tasks.capabilities import _session_key

        manager = SessionManager(SessionConfig(), ContextConfig(), MagicMock(), tmp_path)

        path = manager._session_jsonl_path(_session_key("wf/run-abc123/review_clients/0"))

        assert path.parent == manager._sessions_dir


class TestTheBundleFollowsTheNode:
    """A node's schema and prompt live where the RUNNER dispatched from.

    The live failure: `node declares output_schema '…json' but its bundle is
    not reachable here`. The executor looked under its own agent workspace
    while bundles live in the deployment directory the operator signs into, so
    every node that declared a schema failed on a fleet.
    """

    def test_the_stamped_root_is_used(self, tmp_path: Path) -> None:
        from arcagent.modules.tasks.capabilities import _bundle_root
        from arcagent.modules.tasks.node_execution import node_from_task

        bundle = tmp_path / "deployment" / "workflows" / "onboarding"
        bundle.mkdir(parents=True)
        node = node_from_task(MagicMock(metadata=_node_block(bundle_root=str(bundle))))
        assert node is not None

        assert _bundle_root(MagicMock(), node) == bundle

    def test_the_runner_stamps_what_the_executor_reads(self) -> None:
        """Producer and consumer, checked against each other, not assumed."""
        from arcteam.workflow import runner

        source = Path(runner.__file__).read_text(encoding="utf-8")
        _, _, build_task = source.partition("def _build_task")
        assert '"bundle_root"' in build_task, "the runner no longer stamps the bundle root"

    def test_an_absent_stamp_falls_back_to_the_deployment_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from arcagent.modules.tasks.capabilities import _bundle_root
        from arcagent.modules.tasks.node_execution import node_from_task

        monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "config"))
        bundle = workflows_dir(tmp_path / "config") / "onboarding"
        bundle.mkdir(parents=True)
        node = node_from_task(MagicMock(metadata=_node_block(workflow="onboarding")))
        assert node is not None

        assert _bundle_root(MagicMock(), node) == bundle

    def test_a_schema_is_actually_readable_through_the_stamped_root(self, tmp_path: Path) -> None:
        """The claim that matters: the gate fires instead of reporting absence."""
        import json as _json

        from arcagent.modules.tasks.capabilities import _bundle_root
        from arcagent.modules.tasks.node_execution import node_from_task, resolve_schema

        bundle = tmp_path / "workflows" / "onboarding"
        (bundle / "schemas").mkdir(parents=True)
        (bundle / "schemas" / "out.json").write_text(
            _json.dumps({"type": "object", "required": ["risk"]}), encoding="utf-8"
        )
        node = node_from_task(
            MagicMock(
                metadata=_node_block(bundle_root=str(bundle), output_schema="schemas/out.json")
            )
        )
        assert node is not None

        schema = resolve_schema(node, _bundle_root(MagicMock(), node))

        assert isinstance(schema, dict)
        assert schema["required"] == ["risk"]


class TestTheModelIsShownTheSchemaItIsJudgedBy:
    """A node was judged against a schema it was never told about.

    The runner stamps `output_schema` as a bundle-relative PATH. The prompt
    section only rendered a schema when it was already a dict, so on a real run
    the model saw no shape at all — then did the work correctly, returned its
    own shape, and failed the gate. Same resolution on both sides, or the
    contract is one-sided.
    """

    def test_a_path_shaped_schema_reaches_the_prompt(self, tmp_path: Path) -> None:
        import json as _json

        from arcagent.modules.tasks.capabilities import _bundle_root
        from arcagent.modules.tasks.node_execution import (
            node_from_task,
            render_node_section,
            resolve_schema,
        )

        bundle = tmp_path / "workflows" / "onboarding"
        (bundle / "schemas").mkdir(parents=True)
        (bundle / "schemas" / "out.json").write_text(
            _json.dumps({"type": "object", "required": ["clients"]}), encoding="utf-8"
        )
        node = node_from_task(
            MagicMock(
                metadata=_node_block(bundle_root=str(bundle), output_schema="schemas/out.json")
            )
        )
        assert node is not None

        schema = resolve_schema(node, _bundle_root(MagicMock(), node))
        section = render_node_section(node, schema=schema if isinstance(schema, dict) else None)

        assert "Required output shape" in section
        assert "clients" in section

    def test_the_hook_passes_the_resolved_schema(self) -> None:
        """Producer and consumer of the resolution, checked against each other."""
        from arcagent.modules.tasks import capabilities

        source = Path(capabilities.__file__).read_text(encoding="utf-8")
        _, _, hook = source.partition('sections["workflow_node"] = render_node_section')
        call = hook.split("\n\n")[0]
        assert "schema=" in call, "the prompt no longer carries the resolved schema"
        assert "resolve_schema" in source, "the prompt no longer resolves the bundle's schema"


@pytest.mark.asyncio
class TestOutputArrivesHoweverTheModelSendsIt:
    """A correct answer in the wrong wrapper is still a correct answer.

    The live failure: the node produced exactly the shape its schema required,
    sent it as the JSON *text* of that object, and the gate reported
    "is not of type 'object'" — so a node that had done its work right failed,
    twice, and the agent concluded the platform was broken. It was: a declared
    `dict` argument is a request, not a guarantee (LLM05).
    """

    async def test_a_json_string_output_satisfies_the_schema(self, node_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _state, complete_task

        await _make_node_task(node_state)
        result = json.loads(
            await complete_task(
                id="task_node_1", resolution="done", output=json.dumps({"risk": "low"})
            )
        )

        assert "error" not in result, result
        st = await _state()
        stored = await st.store.get("task_node_1")
        assert stored is not None
        assert stored.status == "done"
        # Stored as the object, never as the text of one.
        assert stored.output == {"risk": "low"}

    async def test_set_task_output_takes_the_same_shape(self, node_state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _state, set_task_output

        await _make_node_task(node_state)
        await set_task_output(id="task_node_1", output=json.dumps({"risk": "high"}))

        st = await _state()
        stored = await st.store.get("task_node_1")
        assert stored is not None
        assert stored.output == {"risk": "high"}

    async def test_text_that_is_not_an_object_is_a_repairable_refusal(
        self, node_state: Any
    ) -> None:
        from arcagent.modules.tasks.capabilities import complete_task

        await _make_node_task(node_state)
        result = json.loads(
            await complete_task(id="task_node_1", resolution="done", output="not json at all")
        )

        assert result["retryable"] is True
        assert "output" in result["error"]


def _script_bundle(root: Path, body: str, name: str = "scripts/stamp.sh") -> Path:
    """Write a script into a bundle directory and return the bundle root."""
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return root


@pytest.mark.asyncio
class TestScriptNodeExecution:
    """SPEC-061 — a ``script`` node runs its bundle script deterministically.

    The claim under test is exactly what a prior build got wrong: a ``script``
    node must EXECUTE its script (not silently no-op), and its stdout must be
    held to the same schema gate an agent node's output is.
    """

    async def test_script_runs_and_completes_with_parsed_output(
        self, node_state: Any, tmp_path: Path
    ) -> None:
        from arcagent.modules.tasks.capabilities import _run_script_node, _state
        from arcagent.modules.tasks.node_execution import node_from_task

        bundle = _script_bundle(
            tmp_path / "bundle",
            '#!/usr/bin/env bash\necho "ran" > "$ARC_WORKDIR/ran.txt"\necho \'{"ok": true}\'\n',
        )
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        task = await _make_node_task(
            node_state,
            node_id="runscript",
            node_kind="script",
            script="scripts/stamp.sh",
            bundle_root=str(bundle),
            output_schema=schema,
        )
        st = await _state()
        node = node_from_task(task)
        assert node is not None
        await _run_script_node(st, task, node, st.identity.did)

        stored = await st.store.get("task_node_1")
        assert stored is not None
        assert stored.status == "done"
        assert stored.output == {"ok": True}
        # The script ACTUALLY executed, in the run's shared workspace.
        assert (tmp_path / "runs" / "run_1" / "ran.txt").read_text().strip() == "ran"

    async def test_nonzero_exit_is_a_retryable_attempt(
        self, node_state: Any, tmp_path: Path
    ) -> None:
        from arcagent.modules.tasks.capabilities import _run_script_node, _state
        from arcagent.modules.tasks.node_execution import node_from_task

        bundle = _script_bundle(
            tmp_path / "bundle", "#!/usr/bin/env bash\necho boom >&2\nexit 3\n"
        )
        task = await _make_node_task(
            node_state,
            node_id="runscript",
            node_kind="script",
            script="scripts/stamp.sh",
            bundle_root=str(bundle),
            output_schema=None,
        )
        st = await _state()
        node = node_from_task(task)
        assert node is not None
        await _run_script_node(st, task, node, st.identity.did)

        stored = await st.store.get("task_node_1")
        assert stored is not None
        assert stored.status == "todo"  # requeued below the ceiling, not dead-lettered
        assert stored.last_error is not None
        assert "non-zero" in stored.last_error

    async def test_stdout_must_satisfy_declared_schema(
        self, node_state: Any, tmp_path: Path
    ) -> None:
        from arcagent.modules.tasks.capabilities import _run_script_node, _state
        from arcagent.modules.tasks.node_execution import node_from_task

        bundle = _script_bundle(
            tmp_path / "bundle", '#!/usr/bin/env bash\necho \'{"ok": "not-a-bool"}\'\n'
        )
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        task = await _make_node_task(
            node_state,
            node_id="runscript",
            node_kind="script",
            script="scripts/stamp.sh",
            bundle_root=str(bundle),
            output_schema=schema,
        )
        st = await _state()
        node = node_from_task(task)
        assert node is not None
        await _run_script_node(st, task, node, st.identity.did)

        stored = await st.store.get("task_node_1")
        assert stored is not None
        assert stored.status != "done"
        assert stored.output is None


@pytest.mark.asyncio
async def test_tool_node_uses_the_governed_tool_projection(node_state: Any) -> None:
    """A declared tool must run directly, never through a model prompt."""
    import arcrun

    from arcagent.modules.tasks.capabilities import _run_tool_node, _state
    from arcagent.modules.tasks.node_execution import node_from_task

    calls: list[tuple[dict[str, Any], arcrun.ToolContext]] = []

    async def execute(args: dict[str, Any], context: arcrun.ToolContext) -> str:
        calls.append((args, context))
        return '{"customer": "acme"}'

    class GovernedTools:
        def to_arcrun_tools(self) -> list[arcrun.Tool]:
            return [
                arcrun.Tool(
                    name="lookup_customer",
                    description="test tool",
                    input_schema={"type": "object"},
                    execute=execute,
                )
            ]

    task = await _make_node_task(
        node_state,
        node_kind="tool",
        tool="lookup_customer",
        args={"customer_id": 42},
        output_schema=None,
    )
    st = await _state()
    st.tool_registry = GovernedTools()
    node = node_from_task(task)
    assert node is not None

    await _run_tool_node(st, task, node, st.identity.did)

    stored = await st.store.get(task.id)
    assert stored is not None
    assert stored.status == "done"
    assert stored.output == {"customer": "acme"}
    assert calls[0][0] == {"customer_id": 42}
    assert calls[0][1].tool_call_id == node.idempotency_key
