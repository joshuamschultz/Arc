"""COMP-001 — the typed model of a ``workflow.toml`` (SPEC-061 REQ-217/REQ-218).

One canonical serialized form is simultaneously the executable definition, the
render source for every viewer, the IDE-editable file, and the diffable audit
record. This module is the in-memory shape of that one form.

Two projections come out of a parsed definition and they are deliberately
different:

* :meth:`WorkflowDefinition.to_document` is *faithful* — it round-trips back
  through :func:`parse_definition` unchanged and is what gets written to disk.
* :meth:`WorkflowDefinition.canonical_document` is *normalized* — nodes sorted
  by id, ``needs`` sorted, empty and absent fields dropped. The content hash a
  signature binds to is taken over this projection, never over raw TOML bytes,
  because TOML has no canonical form: an inline table and an array-of-tables
  are the same data, and a formatter may legitimately rewrite one into the
  other. Hashing bytes would drop a signed workflow to draft for a whitespace
  change.

Nodes carry no LLM wire-control fields. Model and temperature live with the
agent; a node may force the loop ``strategy`` but never the model.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import ErrorDetails

from arcteam.workflow.errors import ValidationIssue, WorkflowParseError

SCHEMA_VERSION = "1.0"
"""The predicate/graph language version this build speaks."""

_SUPPORTED_MAJOR = 1

MAX_NODES = 200
"""Node-count quota. Comparable engines cap far higher; the binding limit here
is the per-agent serial dispatcher, so the format must not permit graphs the
substrate cannot serve (LLM10)."""

MAX_DEFINITION_BYTES = 256 * 1024
"""Size quota for the serialized definition."""

NODE_KINDS: tuple[str, ...] = ("agent", "tool", "script", "router", "gate")

JoinMode = Literal["all", "any"]
RouterMode = Literal["rules", "llm"]
TriggerType = Literal["cron", "interval", "manual"]

_FROZEN = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class ActiveHours(BaseModel):
    """Wall-clock window outside which a trigger does not fire."""

    model_config = _FROZEN

    start: str
    end: str
    timezone: str


class Trigger(BaseModel):
    """How a run starts without a human asking. Absent means manual-only."""

    model_config = _FROZEN

    type: TriggerType
    expression: str | None = None
    interval_s: int | None = Field(default=None, gt=0)
    active_hours: ActiveHours | None = None


class Budget(BaseModel):
    """Run-level ceilings. Enforced by the runner, declared here."""

    model_config = _FROZEN

    tokens: int | None = Field(default=None, gt=0)
    wall_clock_s: int | None = Field(default=None, gt=0)


class InputSpec(BaseModel):
    """The typed run input — the only door for run-time variability.

    The field is ``schema_ref`` in Python (``schema`` shadows a BaseModel
    attribute) and ``schema`` on the wire, which is what the TOML carries.
    """

    model_config = _FROZEN

    schema_ref: str = Field(alias="schema")


class Route(BaseModel):
    """One declared outgoing branch of a router.

    A model never invents a next node: it chooses among these, and the choice
    is recorded. Exactly one route may be the default.
    """

    model_config = _FROZEN

    to: str
    when: str | None = None
    default: bool = False


class NodeBase(BaseModel):
    """Fields every node kind carries.

    ``needs`` are satisfied when each named upstream reaches ``done`` **or**
    ``skipped`` — one non-run terminal state, propagating transitively.
    ``join`` declares fan-in: ``all`` (default) or ``any``.
    """

    model_config = _FROZEN

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    agent: str | None = None
    needs: tuple[str, ...] = ()
    join: JoinMode = "all"
    when: str | None = None
    loop_back_to: str | None = None
    max_iterations: int | None = Field(default=None, gt=0, le=100)
    output_schema: str | None = None
    artifacts: tuple[str, ...] = ()
    strategy: tuple[str, ...] = ()
    timeout_s: int | None = Field(default=None, gt=0)
    max_attempts: int | None = Field(default=None, gt=0, le=20)


class AgentNode(NodeBase):
    """An Infer step: a bounded agent run with a file-referenced prompt."""

    kind: Literal["agent"]
    prompt: str | None = None
    skill: str | None = None


class ToolNode(NodeBase):
    """A Derive step: exactly one declared tool call with wired arguments."""

    kind: Literal["tool"]
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ScriptNode(NodeBase):
    """A Derive step: sandboxed deterministic code."""

    kind: Literal["script"]
    script: str


class RouterNode(NodeBase):
    """Declared branch selection. ``rules`` evaluates predicates; ``llm`` picks
    among the declared route ids under a schema-constrained choice."""

    kind: Literal["router"]
    mode: RouterMode = "rules"
    routes: tuple[Route, ...] = Field(min_length=1)


class GateNode(NodeBase):
    """A human decision. Resolvable only by the control plane, never by a tool."""

    kind: Literal["gate"]
    gate: str


WorkflowNode = Annotated[
    AgentNode | ToolNode | ScriptNode | RouterNode | GateNode,
    Field(discriminator="kind"),
]


class WorkflowDefinition(BaseModel):
    """A parsed, frozen ``workflow.toml``.

    Status and content hash are deliberately absent: they belong to the on-disk
    bundle and its signature, so no amount of successful parsing or validation
    can confer signed status on a definition (REQ-223).
    """

    model_config = _FROZEN

    schema_version: str = SCHEMA_VERSION
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    version: int = Field(default=1, ge=1)
    description: str = ""
    owner: str
    channel: str | None = None
    coordinator: str | None = None
    budget: Budget | None = None
    trigger: Trigger | None = None
    input_spec: InputSpec | None = Field(default=None, alias="input")
    nodes: tuple[WorkflowNode, ...] = Field(min_length=1)

    @property
    def node_ids(self) -> tuple[str, ...]:
        """Node ids in authored order."""
        return tuple(node.id for node in self.nodes)

    def node_by_id(self, node_id: str) -> WorkflowNode:
        """Return the node with ``node_id``. Raises ``KeyError`` if absent."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def to_document(self) -> dict[str, Any]:
        """The faithful TOML document shape — round-trips through the parser."""
        workflow = _drop_empty(
            self.model_dump(
                by_alias=True,
                exclude={"nodes", "trigger", "input_spec"},
                exclude_none=True,
            )
        )
        document: dict[str, Any] = {"workflow": workflow}
        if self.trigger is not None:
            document["trigger"] = _drop_empty(self.trigger.model_dump(exclude_none=True))
        if self.input_spec is not None:
            document["input"] = self.input_spec.model_dump(by_alias=True)
        document["node"] = [
            _drop_empty(node.model_dump(by_alias=True, exclude_none=True)) for node in self.nodes
        ]
        return document

    def canonical_document(self) -> dict[str, Any]:
        """The normalized projection a content hash and signature bind to.

        Presentational choices are erased: node order and ``needs`` order are
        sorted, empty collections and absent fields are dropped. Two documents
        that differ only in formatting produce identical bytes here.
        """
        document = self.to_document()
        nodes = []
        for node in document["node"]:
            normalized = dict(node)
            if "needs" in normalized:
                normalized["needs"] = sorted(normalized["needs"])
            nodes.append(normalized)
        document["node"] = sorted(nodes, key=lambda n: str(n["id"]))
        return document


def _drop_empty(data: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` and empty collections; TOML cannot express either."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if value is None or (isinstance(value, list | tuple | dict) and not value):
            continue
        result[key] = list(value) if isinstance(value, tuple) else value
    return result


def parse_definition(document: dict[str, Any]) -> WorkflowDefinition:
    """Turn a parsed TOML document into a :class:`WorkflowDefinition`.

    Refuses an unknown ``schema_version`` major before anything else: a signed
    artifact cannot be retrofitted with a language version later, and a newer
    instance's definition landing on an older one is the most common import
    failure in comparable systems.

    Raises:
        WorkflowParseError: carrying one :class:`ValidationIssue` per problem.
    """
    workflow = dict(document.get("workflow") or {})
    _refuse_unknown_major(workflow.get("schema_version", SCHEMA_VERSION))

    nodes = document.get("node") or []
    if not isinstance(nodes, list) or not nodes:
        raise WorkflowParseError(
            (
                ValidationIssue(
                    field="node",
                    error="a workflow needs at least one [[node]]",
                    observed=nodes,
                    admissible=("[[node]] with id, kind",),
                ),
            )
        )
    if len(nodes) > MAX_NODES:
        raise WorkflowParseError(
            (
                ValidationIssue(
                    field="node",
                    error=f"a workflow may declare at most {MAX_NODES} nodes",
                    observed=len(nodes),
                    admissible=(f"<= {MAX_NODES} nodes",),
                ),
            )
        )

    payload = dict(workflow)
    payload["nodes"] = nodes
    if "trigger" in document:
        payload["trigger"] = document["trigger"]
    if "input" in document:
        payload["input"] = document["input"]

    try:
        return WorkflowDefinition.model_validate(payload)
    except ValidationError as exc:
        raise WorkflowParseError(_issues_from_pydantic(exc, nodes)) from exc


def _refuse_unknown_major(raw: Any) -> None:
    """Reject a ``schema_version`` this build cannot speak (REQ-217)."""
    text = str(raw)
    major = text.split(".", 1)[0]
    if not major.isdigit() or int(major) != _SUPPORTED_MAJOR:
        raise WorkflowParseError(
            (
                ValidationIssue(
                    field="schema_version",
                    error=(
                        f"this build speaks workflow language major {_SUPPORTED_MAJOR}; "
                        f"refusing an unknown major"
                    ),
                    observed=raw,
                    admissible=(SCHEMA_VERSION, f"{_SUPPORTED_MAJOR}.x"),
                ),
            )
        )


def _issues_from_pydantic(exc: ValidationError, nodes: list[Any]) -> tuple[ValidationIssue, ...]:
    """Translate pydantic errors into the repair-oriented issue shape."""
    issues: list[ValidationIssue] = []
    for error in exc.errors():
        location = [part for part in error["loc"] if part not in NODE_KINDS]
        node_id = _node_id_for(location, nodes)
        field = ".".join(str(part) for part in location if not isinstance(part, int))
        issues.append(
            ValidationIssue(
                node_id=node_id,
                field=field or "workflow",
                error=error["msg"],
                observed=error.get("input"),
                admissible=_admissible_for(field, error),
            )
        )
    return tuple(issues)


def _node_id_for(location: list[Any], nodes: list[Any]) -> str | None:
    """Recover the offending node's id from a pydantic error location."""
    if not location or location[0] != "nodes" or len(location) < 2:
        return None
    index = location[1]
    if not isinstance(index, int) or index >= len(nodes):
        return None
    entry = nodes[index]
    return str(entry.get("id")) if isinstance(entry, dict) and "id" in entry else None


def _admissible_for(field: str, error: ErrorDetails) -> tuple[str, ...]:
    """Name what would have been accepted, which is what drives repair."""
    if field.endswith("kind") or error["type"] == "union_tag_invalid":
        return NODE_KINDS
    if field.endswith("join"):
        return ("all", "any")
    if field.endswith("mode"):
        return ("rules", "llm")
    expected = error.get("ctx", {}).get("expected")
    return (str(expected),) if expected else ()


__all__ = [
    "MAX_DEFINITION_BYTES",
    "MAX_NODES",
    "NODE_KINDS",
    "SCHEMA_VERSION",
    "ActiveHours",
    "AgentNode",
    "Budget",
    "GateNode",
    "InputSpec",
    "JoinMode",
    "NodeBase",
    "Route",
    "RouterMode",
    "RouterNode",
    "ScriptNode",
    "ToolNode",
    "Trigger",
    "TriggerType",
    "WorkflowDefinition",
    "WorkflowNode",
    "parse_definition",
]
