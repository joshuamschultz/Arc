"""Shared fixtures for the workflows builder-tool tests (SPEC-061 COMP-012).

The control plane under the tools is arcteam's (COMP-021) — it owns validation,
versioning, and the draft lifecycle. These tests inject a **recording double**
so the assertions here are about what the arcagent surface does *before* and
*after* the delegation: field allowlists, quota checks ordering, inline-text
normalization, and the draft-status invariant. Graph validation itself is
arcteam's test surface, not this one.

The double deliberately records every call in order so a test can prove that a
quota refusal never reached the control plane at all (quota-before-validation,
LLM10) and that a mutation carried exactly the allowlisted fields.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arctrust import AgentIdentity


class FakeBundle:
    """Stands in for arcteam's ``WorkflowBundle`` (definition + status + hash)."""

    def __init__(self, workflow_id: str, version: int = 1, status: str = "draft") -> None:
        self.status = status
        self.content_hash = f"sha256:{workflow_id}:{version}"
        self.definition = FakeDefinition(workflow_id, version)

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        del mode
        return {
            "status": self.status,
            "content_hash": self.content_hash,
            "definition": self.definition.model_dump(),
        }


class FakeDefinition:
    """Stands in for arcteam's frozen ``WorkflowDefinition``."""

    def __init__(self, workflow_id: str, version: int) -> None:
        self.id = workflow_id
        self.version = version
        self.nodes: list[Any] = []

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        del mode
        return {"id": self.id, "version": self.version, "nodes": []}


class FakeIssue:
    """Stands in for arcteam's ``ValidationIssue`` (pydantic, ``.model_dump()``)."""

    def __init__(self, node_id: str | None, field: str, error: str) -> None:
        self._data = {
            "node_id": node_id,
            "field": field,
            "error": error,
            "observed": None,
            "admissible": (),
        }

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        del mode
        return dict(self._data)


class FakeValidationError(Exception):
    """Stands in for an arcteam graph-validation refusal carrying ``.issues``."""

    def __init__(self, issues: list[FakeIssue]) -> None:
        super().__init__("workflow definition rejected")
        self.issues = tuple(issues)


class StaleEditError(Exception):
    """Stands in for arcteam's ``StaleEditError`` — matched by class NAME.

    The tool surface recognises it through the committed exception name rather
    than by sniffing message text, so this double must carry the real name.
    """


class RecordingControlPlane:
    """Records every control-plane call so ordering can be asserted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.bundles: dict[str, FakeBundle] = {}
        self.raise_on: dict[str, Exception] = {}
        self.next_status = "draft"

    def _record(self, op: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((op, kwargs))
        failure = self.raise_on.get(op)
        if failure is not None:
            raise failure

    def _mutate(self, op: str, workflow_id: str, kwargs: dict[str, Any]) -> FakeBundle:
        self._record(op, kwargs)
        current = self.bundles.get(workflow_id)
        version = 1 if current is None else current.definition.version + 1
        bundle = FakeBundle(workflow_id, version, status=self.next_status)
        self.bundles[workflow_id] = bundle
        return bundle

    async def create(self, **kwargs: Any) -> FakeBundle:
        return self._mutate("create", str(kwargs.get("workflow_id")), kwargs)

    async def add_node(self, **kwargs: Any) -> FakeBundle:
        return self._mutate("add_node", str(kwargs.get("workflow_id")), kwargs)

    async def edit_node(self, **kwargs: Any) -> FakeBundle:
        return self._mutate("edit_node", str(kwargs.get("workflow_id")), kwargs)

    async def remove_node(self, **kwargs: Any) -> FakeBundle:
        return self._mutate("remove_node", str(kwargs.get("workflow_id")), kwargs)

    async def set_trigger(self, **kwargs: Any) -> FakeBundle:
        return self._mutate("set_trigger", str(kwargs.get("workflow_id")), kwargs)

    async def set_channel(self, **kwargs: Any) -> FakeBundle:
        return self._mutate("set_channel", str(kwargs.get("workflow_id")), kwargs)

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self._record("run", kwargs)
        return {"run_id": "run_1", "status": "running"}

    async def cancel_run(self, **kwargs: Any) -> dict[str, Any]:
        self._record("cancel_run", kwargs)
        return {"run_id": kwargs.get("run_id"), "status": "cancelled"}

    async def list(self) -> list[FakeBundle]:
        self._record("list", {})
        return list(self.bundles.values())

    async def inspect(self, **kwargs: Any) -> FakeBundle | None:
        self._record("inspect", kwargs)
        return self.bundles.get(str(kwargs.get("workflow_id")))

    async def runs(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("runs", kwargs)
        return []

    async def run_status(self, **kwargs: Any) -> dict[str, Any]:
        self._record("run_status", kwargs)
        return {"run_id": kwargs.get("run_id"), "status": "running"}

    def ops(self) -> list[str]:
        """The ordered op names recorded so far."""
        return [op for op, _ in self.calls]


@pytest.fixture
def control_plane() -> RecordingControlPlane:
    return RecordingControlPlane()


@pytest.fixture
def workflows_state(
    tmp_path: Path, control_plane: RecordingControlPlane
) -> Iterator[RecordingControlPlane]:
    """Bootstrap the workflows runtime with an injected control plane.

    ``configure()`` is called SYNCHRONOUSLY — the exact shape
    ``core.agent_lifecycle.configure_module_runtimes`` uses in production.
    """
    from arcagent.modules.workflows import _runtime

    _runtime.reset()
    _runtime.configure(
        config={"enabled": True, "max_workflows": 2, "max_nodes": 3},
        workspace=tmp_path,
        identity=AgentIdentity.generate(org="local", agent_type="agent"),
        control_plane=control_plane,
    )
    yield control_plane
    _runtime.reset()
