"""Derived view of a run: what has happened, read back from durable state.

The runner keeps no progress cursor in memory. Everything it needs to decide
the next step is re-derived on every tick from two durable sources:

* the **task rows** scoped to the run — the journal of node execution, where
  ``status=done`` plus ``output`` IS the memoization record (Restate/Inngest
  model, not a full replay), and
* the **Run record's path taken** — the ordered trace of materializations,
  router choices, skips, loop iterations, gate decisions, and settlements.

Re-deriving rather than remembering is what makes a restart safe: a runner that
died mid-materialization comes back, reads the same two sources, and reaches the
same conclusions, so the frontier is completed instead of duplicated.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from arcstore.tasks import Task

from .runner_contracts import IN_FLIGHT_TASK_STATUSES

NodeStatus = Literal["absent", "in_flight", "done", "failed", "skipped"]


@dataclass(frozen=True)
class NodeInstance:
    """One materialized attempt of a node at one loop iteration."""

    node_id: str
    iteration: int
    task: Task

    @property
    def output(self) -> dict[str, Any]:
        return dict(self.task.output or {})


class RunState:
    """The whole derived picture of one run. Pure; owns no I/O."""

    def __init__(self, tasks: Sequence[Task], path: Sequence[Mapping[str, Any]]) -> None:
        self.instances: dict[str, list[NodeInstance]] = {}
        for task in tasks:
            node_id = str(task.metadata.get("node_id", ""))
            if not node_id:
                continue
            iteration = int(task.metadata.get("iteration", 0))
            self.instances.setdefault(node_id, []).append(
                NodeInstance(node_id=node_id, iteration=iteration, task=task)
            )
        for group in self.instances.values():
            group.sort(key=lambda instance: instance.iteration)

        self.skips: set[tuple[str, int]] = set()
        self.routes: dict[tuple[str, int], str] = {}
        self.settled: set[tuple[str, int]] = set()
        self.gates: set[tuple[str, int]] = set()
        self.loops: set[tuple[str, int]] = set()
        self.materialized: set[tuple[str, int]] = set()
        for entry in path:
            key = (str(entry.get("node_id", "")), int(entry.get("iteration", 0)))
            kind = entry.get("kind")
            if kind == "materialized":
                self.materialized.add(key)
            elif kind == "skipped":
                self.skips.add(key)
            elif kind == "route":
                self.routes[key] = str(entry.get("chosen", ""))
            elif kind == "settled":
                self.settled.add(key)
            elif kind == "gate":
                self.gates.add(key)
            elif kind == "loop":
                self.loops.add(key)

    # -- reads ------------------------------------------------------------

    def latest(self, node_id: str) -> NodeInstance | None:
        group = self.instances.get(node_id)
        return group[-1] if group else None

    def materialized_count(self, node_id: str) -> int:
        """How many times this node has been materialized (its loop counter)."""
        return len(self.instances.get(node_id, []))

    def highest_iteration(self, node_id: str) -> int:
        """Highest iteration this node has reached, materialized or skipped."""
        reached = [instance.iteration for instance in self.instances.get(node_id, [])]
        reached.extend(iteration for node, iteration in self.skips if node == node_id)
        reached.extend(iteration for node, iteration in self.routes if node == node_id)
        return max(reached, default=-1)

    def status_at(self, node_id: str, iteration: int) -> NodeStatus:
        """The node's state at one iteration. A router is done once it has chosen."""
        if (node_id, iteration) in self.routes:
            return "done"
        if (node_id, iteration) in self.skips:
            return "skipped"
        for instance in self.instances.get(node_id, []):
            if instance.iteration != iteration:
                continue
            if instance.task.status == "done":
                return "done"
            if instance.task.status == "failed":
                return "failed"
            return "in_flight"
        return "absent"

    def terminal_state(self, node_id: str) -> tuple[NodeStatus, int]:
        """The node's latest terminal state and the iteration it happened at."""
        iteration = self.highest_iteration(node_id)
        if iteration < 0:
            return "absent", 0
        return self.status_at(node_id, iteration), iteration

    def outputs(self) -> dict[str, dict[str, Any]]:
        """Validated upstream outputs, latest iteration wins — the wiring source."""
        return {
            node_id: group[-1].output
            for node_id, group in self.instances.items()
            if group[-1].task.status == "done"
        }

    def in_flight(self) -> list[Task]:
        return [
            instance.task
            for group in self.instances.values()
            for instance in group
            if instance.task.status in IN_FLIGHT_TASK_STATUSES
        ]

    def failures(self) -> list[NodeInstance]:
        return [
            instance
            for group in self.instances.values()
            for instance in group
            if instance.task.status == "failed"
        ]

    def scope(self, run_input: Mapping[str, Any]) -> dict[str, Any]:
        """The predicate/wiring scope: ``$nodes.<id>.output.<field>`` and ``$input.*``."""
        return {
            "nodes": {node_id: {"output": output} for node_id, output in self.outputs().items()},
            "input": dict(run_input),
        }

    # -- in-tick mutation, mirroring what is appended to the path ----------
    #
    # These keep the derived view consistent with the journal entry written in
    # the same breath, so a decision made early in a pass is visible to the
    # nodes decided later in it. They are a convergence speed-up, not a
    # correctness guard: the durable path is re-read at the top of every pass,
    # so dropping one of these costs an extra pass and changes no outcome.
    # Verified by mutation — see the equivalence note in the test suite.

    def record_skip(self, node_id: str, iteration: int) -> None:
        self.skips.add((node_id, iteration))

    def record_route(self, node_id: str, iteration: int, chosen: str) -> None:
        self.routes[(node_id, iteration)] = chosen

    def record_loop(self, node_id: str, iteration: int) -> None:
        self.loops.add((node_id, iteration))

    def record_instance(self, node_id: str, iteration: int, task: Task) -> None:
        """Idempotent: a row already derived from the store is not added twice."""
        group = self.instances.setdefault(node_id, [])
        if any(instance.iteration == iteration for instance in group):
            return
        group.append(NodeInstance(node_id=node_id, iteration=iteration, task=task))
        group.sort(key=lambda instance: instance.iteration)


__all__ = ["NodeInstance", "NodeStatus", "RunState"]
