"""Workflow node execution adapter — SPEC-061 COMP-014 / COMP-015.

This is where typed handoff actually fires. A workflow node is materialised by
the runner as an ordinary arcstore task row carrying a ``workflow`` block in its
metadata; the owning agent's existing dispatch loop claims it like any other
task. Everything that makes it a *node* rather than a task lives here:

- **Upstream outputs and node instructions reach the model through the
  assemble-prompt sections seam**, never by mutating context directly —
  compaction is a single discrete path and a second writer breaks it.
  Values are bound as a typed JSON block under a labelled section (REQ-239):
  nothing is interpolated into a command string or spliced into prose.
- **A declared skill is activated deterministically** from the capability
  registry, not by hoping the model calls ``use_skill``.
- **A declared strategy list is passed to the loop** as its allowed set;
  absent, the reactive strategy is pinned (REQ-243).
- **Output is validated against the node's declared schema before it is
  visible downstream** (REQ-237). A schema failure is a RETRYABLE node
  failure — never a pass-forward, because a downstream node consuming an
  unvalidated value is the whole failure mode typed handoff exists to prevent.
- **Declared artifacts must exist on disk** for completion to count, and the
  retry message names the tool that produces each missing file (REQ-238).
- **A per-attempt idempotency key** ``(run_id, node_id, attempt)`` is bound to
  the dispatch context so a retried node cannot repeat an external side effect
  (REQ-242).

The node's ``instructions`` and ``output_schema`` arrive already RESOLVED on the
task row: the runner reads them from the signed bundle against a verified
manifest and stamps the values. This module never re-reads a referenced file —
doing so would execute a hybrid of two versions the moment a file drifted.
"""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# The metadata key the runner stamps a workflow node under.
WORKFLOW_META = "workflow"

# Bound on a node output threaded into a downstream prompt. An uncapped output
# is both a token blowout and an injection surface (DESIGN §4 performance note).
MAX_OUTPUT_CHARS = 262_144

# Which tool produces which kind of declared artifact. Naming the producer is
# what makes the retry message actionable instead of a bare "file missing".
_ARTIFACT_PRODUCER = "write"


class WorkflowNode(BaseModel):
    """The resolved node spec carried on a materialised task row."""

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    run_id: str
    node_id: str
    attempt: int = 1
    kind: str = "agent"
    # Resolved node instructions (the signed ``prompt`` file's content), not a
    # path — see the module docstring on why this module never re-reads files.
    instructions: str = ""
    skill: str | None = None
    strategy: list[str] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    artifacts: list[str] = Field(default_factory=list)
    # Validated outputs of the upstream nodes this one needs, keyed by node id.
    upstream: dict[str, Any] = Field(default_factory=dict)
    # The run's accumulated lethal-trifecta legs at the moment this node was
    # materialised (COMP-015). Seeds this node's fresh session so a per-node
    # session cannot reset the run's accumulation.
    accumulated_legs: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class NodeAttempt:
    """The per-attempt identity a dispatched node carries into tool calls."""

    run_id: str
    node_id: str
    attempt: int

    @property
    def idempotency_key(self) -> str:
        """Stable key for this exact attempt — a retry produces a different one."""
        return f"{self.run_id}:{self.node_id}:{self.attempt}"


# The node being executed by the running dispatch. A ContextVar so a tool call
# inside the loop inherits it (the same guarantee ``turn_context`` relies on)
# and so concurrent dispatches never see each other's node.
_current_node: contextvars.ContextVar[WorkflowNode | None] = contextvars.ContextVar(
    "arcagent_workflow_node", default=None
)


def current_node() -> WorkflowNode | None:
    """The workflow node bound to the running dispatch, or None."""
    return _current_node.get()


def bind_node(node: WorkflowNode) -> contextvars.Token[WorkflowNode | None]:
    """Bind the node for this dispatch; returns a token for :func:`reset_node`."""
    return _current_node.set(node)


def reset_node(token: contextvars.Token[WorkflowNode | None]) -> None:
    """Restore the previous node binding."""
    _current_node.reset(token)


def idempotency_key() -> str | None:
    """The running dispatch's per-attempt idempotency key, or None (REQ-242).

    Read by any tool whose effect is externally visible, so a retried node
    re-executing the same call is recognisable as the same attempt rather than
    a second, duplicated side effect.
    """
    node = _current_node.get()
    if node is None:
        return None
    return NodeAttempt(node.run_id, node.node_id, node.attempt).idempotency_key


def node_from_task(task: Any) -> WorkflowNode | None:
    """Parse the workflow node off a task row, or None for an ordinary task."""
    metadata = getattr(task, "metadata", None) or {}
    block = metadata.get(WORKFLOW_META)
    if not isinstance(block, dict):
        return None
    try:
        return WorkflowNode.model_validate(block)
    except ValueError:
        # A malformed block is a corrupt row, not a node: treat it as an
        # ordinary task rather than crashing the whole dispatch loop.
        return None


def render_node_section(node: WorkflowNode, skill_body: str | None = None) -> str:
    """The prompt section carrying node instructions and upstream outputs.

    Upstream values are rendered as a typed JSON block under an explicit,
    per-node label — bound into a prompt SECTION, never interpolated into a
    command or spliced into prose (REQ-239). The model reads them as data it was
    handed, with the producing node named, so a value can always be traced back.
    """
    lines = [f"## Workflow node `{node.node_id}` (run {node.run_id}, attempt {node.attempt})"]
    if node.instructions:
        lines.extend(["", "### Instructions", node.instructions])
    if node.upstream:
        lines.extend(["", "### Upstream outputs (typed, validated)"])
        for upstream_id, value in sorted(node.upstream.items()):
            rendered = json.dumps(value, indent=2, default=str)[:MAX_OUTPUT_CHARS]
            lines.extend([f"`{upstream_id}`:", "```json", rendered, "```"])
    if node.output_schema is not None:
        lines.extend(
            [
                "",
                "### Required output shape",
                "Call `complete_task` with an `output` matching this JSON Schema. "
                "An output that does not match is a retryable failure, not a result.",
                "```json",
                json.dumps(node.output_schema, indent=2)[:MAX_OUTPUT_CHARS],
                "```",
            ]
        )
    if node.artifacts:
        listed = ", ".join(f"`{name}`" for name in node.artifacts)
        lines.extend(
            ["", "### Required artifacts", f"These files must exist when you finish: {listed}."]
        )
    if skill_body:
        lines.extend(["", "### Activated skill", skill_body])
    return "\n".join(lines)


def validate_output(node: WorkflowNode, output: dict[str, Any] | None) -> str | None:
    """Validate a node's output against its declared schema (REQ-237).

    Returns None when the output is acceptable, or a message describing the
    violation. The caller treats a message as a RETRYABLE node failure — the
    value is never recorded, so it can never be visible downstream.
    """
    if node.output_schema is None:
        return None
    if output is None:
        return "node declares an output_schema but completed with no output"
    import jsonschema

    try:
        jsonschema.validate(instance=output, schema=node.output_schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path) or "<root>"
        return f"output does not satisfy output_schema at '{path}': {exc.message}"
    except jsonschema.SchemaError as exc:
        return f"node output_schema is itself invalid: {exc.message}"
    return None


def escaping_artifacts(node: WorkflowNode, root: Path) -> list[str]:
    """Declared artifact paths that resolve outside ``root``.

    The definition validator refuses absolute and ``..`` artifact paths at
    authoring time — but a hand-edited bundle loaded straight off disk never
    went through it, and the runner passes ``artifacts`` into the task row as
    opaque strings. This adapter is the first place they become real filesystem
    operations, so it must not rely on an upstream check it cannot prove
    happened: an unconfined ``../../etc/passwd`` would let a node report itself
    complete by pointing at a file it never produced, and leak whether arbitrary
    paths exist. ``confine`` is the same guard the definition store uses, reused
    rather than re-implemented — one escape check, one place to get it right.
    """
    return [name for name in node.artifacts if _confined(root, name) is None]


def _confined(root: Path, reference: str) -> Path | None:
    """Resolve ``reference`` under ``root``, or None if it escapes.

    Falls back to a local check when ``arcteam.workflow`` is absent — an
    escaping path must be refused on every deployment, including one with no
    workflow engine installed, because refusing is the safe direction.
    """
    try:
        from arcteam.workflow import confine
    except ImportError:
        candidate = Path(reference)
        if candidate.is_absolute():
            return None
        resolved = (root / candidate).resolve()
        return resolved if resolved == root or root in resolved.parents else None
    confined: Path | None = confine(root, reference)
    return confined


def missing_artifacts(node: WorkflowNode, root: Path) -> list[str]:
    """Declared artifacts that do not exist under ``root`` (REQ-238).

    Only confined paths are stat-ed. An escaping path is reported by
    :func:`escaping_artifacts` and refused there — never resolved, never
    touched.
    """
    missing = []
    for name in node.artifacts:
        target = _confined(root, name)
        if target is not None and not target.exists():
            missing.append(name)
    return missing


def artifact_failure(missing: list[str]) -> str:
    """The retry message for absent artifacts, naming each producing tool."""
    named = "; ".join(
        f"'{name}' (produce it with the `{_ARTIFACT_PRODUCER}` tool)" for name in missing
    )
    return f"node is not complete — declared artifacts are missing: {named}"


def artifact_escape_failure(escaping: list[str]) -> str:
    """The refusal for artifact paths that leave the node's working root."""
    named = ", ".join(f"'{name}'" for name in escaping)
    return (
        f"node declares artifacts outside its working root and is refused: {named} — "
        "an artifact path must be relative and must stay inside the workspace"
    )


def allowed_strategies(node: WorkflowNode) -> list[str]:
    """The loop's allowed strategy set for this node (REQ-243).

    A declared list is passed through verbatim; an absent one pins the reactive
    strategy rather than leaving the loop free to pick — a workflow node's whole
    point is that it runs the same way twice.
    """
    return list(node.strategy) if node.strategy else ["react"]


__all__ = [
    "MAX_OUTPUT_CHARS",
    "WORKFLOW_META",
    "NodeAttempt",
    "WorkflowNode",
    "allowed_strategies",
    "artifact_escape_failure",
    "artifact_failure",
    "bind_node",
    "current_node",
    "escaping_artifacts",
    "idempotency_key",
    "missing_artifacts",
    "node_from_task",
    "render_node_section",
    "reset_node",
    "validate_output",
]
