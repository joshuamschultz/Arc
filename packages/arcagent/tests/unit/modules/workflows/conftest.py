"""Shared fixtures for the workflows builder-tool tests (SPEC-061 COMP-012).

The control plane under the tools is arcteam's (COMP-021) — it owns validation,
versioning, and the draft lifecycle. These tests inject a **recording double**
shaped exactly like the landed ``WorkflowControlPlane``: ``create(document)`` /
``edit(workflow_id, document, expected_version=...)`` returning a
``ControlPlaneResult``. The assertions here are about what the arcagent surface
does before and after that delegation — field allowlists, quota ordering,
inline-text normalization, and the draft-status invariant. Graph validation
itself is arcteam's test surface, not this one.

The double records every call in order so a test can prove that a quota refusal
never reached the control plane at all, and that a mutation carried exactly the
allowlisted fields.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arctrust import AgentIdentity


class FakeDefinition:
    """Stands in for arcteam's frozen ``WorkflowDefinition``."""

    def __init__(self, workflow_id: str, version: int, nodes: list[dict[str, Any]]) -> None:
        self.id = workflow_id
        self.version = version
        self.nodes = nodes

    def model_dump(self, mode: str = "python", exclude_none: bool = False) -> dict[str, Any]:
        del mode, exclude_none
        return {"id": self.id, "version": self.version, "nodes": [dict(n) for n in self.nodes]}


class FakeBundle:
    """Stands in for arcteam's ``WorkflowBundle`` (definition + status + hash)."""

    def __init__(
        self,
        workflow_id: str,
        version: int = 1,
        status: str = "draft",
        nodes: list[dict[str, Any]] | None = None,
    ) -> None:
        self.status = status
        self.content_hash = f"sha256:{workflow_id}:{version}"
        self.definition = FakeDefinition(workflow_id, version, nodes or [])

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        del mode
        return {
            "status": self.status,
            "content_hash": self.content_hash,
            "definition": self.definition.model_dump(),
        }


class FakeIssue:
    """Stands in for arcteam's ``ValidationIssue`` (pydantic, ``.model_dump()``)."""

    def __init__(self, node_id: str | None, field: str, error: str) -> None:
        self._data: dict[str, Any] = {
            "node_id": node_id,
            "field": field,
            "error": error,
            "observed": None,
            "admissible": (),
        }

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        del mode
        return dict(self._data)


class FakeResult:
    """Stands in for ``ControlPlaneResult``."""

    def __init__(
        self,
        ok: bool,
        bundle: FakeBundle | None = None,
        run: dict[str, Any] | None = None,
        errors: tuple[FakeIssue, ...] = (),
    ) -> None:
        self.ok = ok
        self.bundle = bundle
        self.run = run
        self.errors = errors


class StaleEditError(Exception):
    """Stands in for arcteam's ``StaleEditError`` — matched by class NAME.

    The tool surface recognises it through the committed exception name rather
    than by sniffing message text, so this double must carry the real name.
    """


class FakeDefinitionStore:
    """Stands in for arcteam's ``DefinitionStore`` (the read half)."""

    def __init__(self) -> None:
        self.bundles: dict[str, FakeBundle] = {}

    def list_ids(self, *, include_archived: bool = False) -> tuple[str, ...]:
        del include_archived
        return tuple(self.bundles)

    def load(self, workflow_id: str) -> FakeBundle:
        return self.bundles[workflow_id]

    def load_version(self, workflow_id: str, version: int) -> FakeDefinition:
        return FakeDefinition(workflow_id, version, [])


class RecordingControlPlane:
    """Records every control-plane call so ordering can be asserted."""

    def __init__(self, definitions: FakeDefinitionStore) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Companion bodies the last mutation carried (path -> bytes).
        self.files: dict[str, bytes] = {}
        self.definitions = definitions
        self.raise_on: dict[str, Exception] = {}
        self.refuse_with: tuple[FakeIssue, ...] = ()
        self.next_status = "draft"

    def _write(self, op: str, workflow_id: str, document: dict[str, Any]) -> FakeResult:
        self.calls.append((op, {"workflow_id": workflow_id, "document": document}))
        failure = self.raise_on.get(op)
        if failure is not None:
            raise failure
        if self.refuse_with:
            return FakeResult(ok=False, errors=self.refuse_with)
        current = self.definitions.bundles.get(workflow_id)
        version = 1 if current is None else current.definition.version + 1
        bundle = FakeBundle(
            workflow_id, version, status=self.next_status, nodes=list(document.get("node", []))
        )
        self.definitions.bundles[workflow_id] = bundle
        return FakeResult(ok=True, bundle=bundle)

    async def create(
        self,
        document: dict[str, Any],
        *,
        actor_did: str,
        files: dict[str, bytes] | None = None,
    ) -> FakeResult:
        del actor_did
        self.files = dict(files or {})
        return self._write("create", str(document["workflow"]["id"]), document)

    async def edit(
        self,
        workflow_id: str,
        document: dict[str, Any],
        *,
        expected_version: int,
        actor_did: str,
        reason: str,
        files: dict[str, bytes] | None = None,
    ) -> FakeResult:
        del actor_did, reason
        self.files = dict(files or {})
        self.calls.append(("edit_meta", {"expected_version": expected_version}))
        return self._write("edit", workflow_id, document)

    async def run(
        self,
        workflow_id: str,
        *,
        input: dict[str, Any],  # noqa: A002 - mirrors the real control-plane signature
        actor_did: str,
    ) -> FakeResult:
        del actor_did
        self.calls.append(("run", {"workflow_id": workflow_id, "input": input}))
        return FakeResult(ok=True, run={"run_id": "run_1", "status": "running"})

    async def cancel(self, run_id: str, *, actor_did: str) -> FakeResult:
        del actor_did
        self.calls.append(("cancel", {"run_id": run_id}))
        return FakeResult(ok=True, run={"run_id": run_id, "status": "cancelled"})

    def ops(self) -> list[str]:
        """The ordered op names recorded so far."""
        return [op for op, _ in self.calls]

    def last(self, op: str) -> dict[str, Any]:
        """The payload of the most recent ``op`` call."""
        return next(payload for name, payload in reversed(self.calls) if name == op)


@pytest.fixture
def definitions() -> FakeDefinitionStore:
    return FakeDefinitionStore()


@pytest.fixture
def control_plane(definitions: FakeDefinitionStore) -> RecordingControlPlane:
    return RecordingControlPlane(definitions)


@pytest.fixture
def workflows_state(
    tmp_path: Path,
    control_plane: RecordingControlPlane,
    definitions: FakeDefinitionStore,
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
        definitions=definitions,
    )
    yield control_plane
    _runtime.reset()
