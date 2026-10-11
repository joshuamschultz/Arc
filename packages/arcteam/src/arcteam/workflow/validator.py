"""COMP-002 — whole-graph static validation (SPEC-061 REQ-219/REQ-220).

Every authoring surface — the agent's builder tools, a hand edit in an IDE, the
dashboard editor — runs this one validator over the *whole* graph before any
write. Node-by-node validation while writing is what lets a half-written graph
reach disk, so validation is always in-memory and always complete.

The checks that matter most are the ones comparable engines defer to runtime
and then debug forever:

* **The exclusive-route merge.** A node whose ``needs`` span mutually exclusive
  routes of one router can never become ready, because only one of those
  branches will ever run. Fan-in after a router is not supported: it is
  rejected here, statically, and each branch continues on its own.
* **Cycles.** A workflow is a DAG. Any cycle is refused; a retry is an operator
  action on a failed node, never a loop in the definition.
* **Statically unsatisfiable output references.** A node reading
  ``$nodes.X.output.*`` where X is not a transitive dependency — or sits on a
  branch the reader can never co-occur with — can never bind that value.

Every rejection is a :class:`~arcteam.workflow.errors.ValidationIssue` naming
the node, the field, the observed value, and the admissible alternatives, and
the whole list comes back at once so a repair pass sees every problem.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from arcteam.workflow.errors import (
    PredicateError,
    UnresolvableReferenceError,
    ValidationIssue,
)
from arcteam.workflow.models import (
    APPROVER_ROLE_PREFIX,
    MAX_DEFINITION_BYTES,
    MAX_NODES,
    AgentNode,
    GateNode,
    HumanInputNode,
    RouterNode,
    ScriptNode,
    ToolNode,
    WorkflowDefinition,
    WorkflowNode,
)
from arcteam.workflow.predicates import PathRef, parse_predicate, paths_in
from arcteam.workflow.resolver import (
    embedded_reference_strings,
    malformed_reference_strings,
    references_in,
)
from arcteam.workflow.runner_contracts import HUMAN_STEP_KINDS, HumanStepSpec, WorkflowSpec
from arcteam.workflow.schema_check import SchemaRefusedError, check, load_schema

RESERVED_INPUT_PROPERTY = "arc_trigger"
"""The run-input key the runner writes itself; no input or event schema may claim it."""


class KnownReferences(BaseModel):
    """The roster a definition's references are checked against.

    An empty set for one kind means "this deployment has no roster of that
    kind" and skips it. Partial knowledge is the normal case — a caller can
    name every registered agent long before anything can enumerate every tool
    in the fleet — and rejecting every tool node because nobody could list the
    tools would make the check unusable exactly where it is most wanted.
    ``known=None`` skips all of them.

    ``users`` is the DIDs of active registry entities of type ``user``: an
    ``escalate_to`` DID must be one of them, never an agent or a stranger.
    Unlike the other kinds it fails closed — an EMPTY set refuses every
    escalation DID, because nobody is left to escalate to. ``None`` means the
    author had no roster to read (no team bus); run start, which always asks
    the live registry, is then the check (REQ-622).

    ``user_roles`` is the roles those users hold. An ``escalate_to = "role:X"``
    must be one of them — a role only an agent holds is refused — and, like
    ``users``, an empty set refuses and ``None`` falls back to ``roles``.
    Approver roles are still checked against ``roles``.
    """

    model_config = ConfigDict(frozen=True)

    agents: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()
    skills: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    users: frozenset[str] | None = None
    user_roles: frozenset[str] | None = None


def validate_definition(
    definition: WorkflowDefinition,
    *,
    known: KnownReferences | None = None,
    bundle_root: Path | None = None,
    raw_size_bytes: int | None = None,
    pending_files: frozenset[str] = frozenset(),
) -> tuple[ValidationIssue, ...]:
    """Validate the whole graph. An empty tuple means the definition is sound.

    Args:
        definition: The parsed definition.
        known: Roster of real agents, tools, and skills. ``None`` skips it.
        bundle_root: Bundle directory, so schema/prompt/script references can
            be resolved and confined. ``None`` skips file resolution.
        raw_size_bytes: Serialized size, for the definition quota.
        pending_files: Bundle-relative paths that are about to be written and
            so count as present. This is what lets a caller validate *before*
            touching the disk, which is the only way a refused save can leave
            the bundle untouched.

    Returns:
        Every problem found, each naming node, field, observed value, and
        admissible alternatives.
    """
    issues = list(_check_quotas(definition, raw_size_bytes))
    duplicates = tuple(_check_duplicate_ids(definition))
    if duplicates:
        return tuple(issues + list(duplicates))

    structural = tuple(_check_structure(definition))
    issues += structural
    issues += _check_predicates(definition)
    issues += _check_node_options(definition)
    issues += _check_known(definition, known)
    issues += _check_files(definition, bundle_root, pending_files)
    issues += _check_schema_contents(definition, bundle_root)
    if structural:
        return tuple(issues)

    graph = _Graph(definition)
    cycles = tuple(_check_cycles(definition, graph))
    issues += cycles
    if cycles:
        return tuple(issues)

    issues += _check_exclusive_needs(definition, graph)
    issues += _check_output_references(definition, graph)
    issues += _check_prompt_references(definition, graph, bundle_root)
    return tuple(issues)


# --- quotas and identity -----------------------------------------------------


def _check_quotas(
    definition: WorkflowDefinition, raw_size_bytes: int | None
) -> Iterable[ValidationIssue]:
    if len(definition.nodes) > MAX_NODES:
        yield ValidationIssue(
            field="node",
            error=f"a workflow may declare at most {MAX_NODES} nodes",
            observed=len(definition.nodes),
            admissible=(f"<= {MAX_NODES} nodes",),
        )
    if raw_size_bytes is not None and raw_size_bytes > MAX_DEFINITION_BYTES:
        yield ValidationIssue(
            field="size",
            error=f"the definition exceeds the {MAX_DEFINITION_BYTES} byte quota",
            observed=raw_size_bytes,
            admissible=(f"<= {MAX_DEFINITION_BYTES} bytes",),
        )


def _check_duplicate_ids(definition: WorkflowDefinition) -> Iterable[ValidationIssue]:
    """Duplicate ids are checked alone: the graph is meaningless without them."""
    seen: set[str] = set()
    for node in definition.nodes:
        if node.id in seen:
            yield ValidationIssue(
                node_id=node.id,
                field="id",
                error="node ids must be unique within a workflow",
                observed=node.id,
                admissible=("a node id not already used",),
            )
        seen.add(node.id)


# --- structural edges --------------------------------------------------------


def _check_structure(definition: WorkflowDefinition) -> Iterable[ValidationIssue]:
    """Edge soundness. Anything here makes graph analysis meaningless."""
    ids = set(definition.node_ids)
    for node in definition.nodes:
        yield from _check_needs(node, ids)
        if isinstance(node, RouterNode):
            yield from _check_routes(node, definition, ids)


def _check_needs(node: WorkflowNode, ids: set[str]) -> Iterable[ValidationIssue]:
    for need in node.needs:
        if need == node.id:
            yield ValidationIssue(
                node_id=node.id,
                field="needs",
                error="a node cannot depend on itself",
                observed=need,
                admissible=tuple(sorted(ids - {node.id})),
            )
        elif need not in ids:
            yield ValidationIssue(
                node_id=node.id,
                field="needs",
                error="needs names a node that does not exist",
                observed=need,
                admissible=tuple(sorted(ids - {node.id})),
            )


def _check_routes(
    router: RouterNode, definition: WorkflowDefinition, ids: set[str]
) -> Iterable[ValidationIssue]:
    """Routes must reach real nodes that gate on this router, with one default."""
    defaults = [route for route in router.routes if route.default]
    if len(defaults) > 1:
        yield ValidationIssue(
            node_id=router.id,
            field="routes",
            error="a router may declare at most one default route",
            observed=[route.to for route in defaults],
            admissible=("exactly one route with default = true",),
        )
    for route in router.routes:
        if route.to not in ids:
            yield ValidationIssue(
                node_id=router.id,
                field="routes",
                error="a route names a node that does not exist",
                observed=route.to,
                admissible=tuple(sorted(ids - {router.id})),
            )
            continue
        if router.id not in definition.node_by_id(route.to).needs:
            yield ValidationIssue(
                node_id=router.id,
                field="routes",
                error=(
                    f"route target {route.to!r} must declare this router in its needs, "
                    f"or the runner cannot gate it"
                ),
                observed=route.to,
                admissible=(f'needs = ["{router.id}"] on node {route.to!r}',),
            )
        if router.mode == "rules" and route.when is None and not route.default:
            yield ValidationIssue(
                node_id=router.id,
                field="routes",
                error="every route of a rules router needs a when predicate or default = true",
                observed=route.to,
                admissible=('when = "..."', "default = true"),
            )


# --- node-level options ------------------------------------------------------


def _check_node_options(definition: WorkflowDefinition) -> Iterable[ValidationIssue]:
    for node in definition.nodes:
        if node.on_failure == "continue" and node.kind in ("gate", "router"):
            yield ValidationIssue(
                node_id=node.id,
                field="on_failure",
                error=(
                    "a failed gate is a rejection and a failed router chose no route; "
                    "the run may not go on past either"
                ),
                observed=node.on_failure,
                admissible=("fail_run", "skip_dependents"),
            )
        if isinstance(node, HumanInputNode) and node.output_schema is None:
            yield ValidationIssue(
                node_id=node.id,
                field="output_schema",
                error="a human_input node must declare the schema its answer is checked against",
                observed=None,
                admissible=('output_schema = "<path to a JSON Schema in the bundle>"',),
            )
        yield from _check_artifact_paths(node)


def _check_artifact_paths(node: WorkflowNode) -> Iterable[ValidationIssue]:
    """Declared artifacts stay inside the run's working tree."""
    for artifact in node.artifacts:
        path = Path(artifact)
        if path.is_absolute() or ".." in path.parts:
            yield ValidationIssue(
                node_id=node.id,
                field="artifacts",
                error="an artifact path must be relative and must not escape the working tree",
                observed=artifact,
                admissible=("a relative path with no '..' segment",),
            )


# --- predicates --------------------------------------------------------------


def _check_predicates(definition: WorkflowDefinition) -> Iterable[ValidationIssue]:
    for node in definition.nodes:
        if node.when is not None:
            yield from _parse_or_issue(node.id, "when", node.when)
        if isinstance(node, RouterNode):
            for route in node.routes:
                if route.when is not None:
                    yield from _parse_or_issue(node.id, "routes", route.when)


def _parse_or_issue(node_id: str, field: str, expression: str) -> Iterable[ValidationIssue]:
    try:
        parse_predicate(expression)
    except PredicateError as exc:
        yield ValidationIssue(
            node_id=node_id,
            field=field,
            error=f"the predicate is not in the workflow predicate grammar: {exc}",
            observed=expression,
            admissible=(
                "comparison of $nodes.<id>.output.<field> or $input.<field> "
                "against a literal, combined with and/or/not",
            ),
        )


# --- roster and files --------------------------------------------------------


def approver_roles(approvers: Iterable[str]) -> frozenset[str]:
    """The role names (prefix stripped) among a gate's ``approvers``."""
    return frozenset(
        a[len(APPROVER_ROLE_PREFIX) :] for a in approvers if a.startswith(APPROVER_ROLE_PREFIX)
    )


def _check_known(
    definition: WorkflowDefinition, known: KnownReferences | None
) -> Iterable[ValidationIssue]:
    if known is None:
        return
    if known.agents and definition.owner not in known.agents:
        yield ValidationIssue(
            field="owner",
            error="the workflow owner is not a registered agent",
            observed=definition.owner,
            admissible=tuple(sorted(known.agents)),
        )
    for node in definition.nodes:
        if known.agents and node.agent is not None and node.agent not in known.agents:
            yield _unknown(node.id, "agent", node.agent, known.agents)
        if known.tools and isinstance(node, ToolNode) and node.tool not in known.tools:
            yield _unknown(node.id, "tool", node.tool, known.tools)
        if known.skills and isinstance(node, AgentNode) and node.skill is not None:
            if node.skill not in known.skills:
                yield _unknown(node.id, "skill", node.skill, known.skills)
        if known.users is not None and isinstance(node, GateNode | HumanInputNode):
            yield from _check_escalation_user(node, known.users)
    if known.roles:
        yield from _approver_role_issues(definition, known.roles)
    escalation_roles = known.roles if known.user_roles is None else known.user_roles
    if known.user_roles is not None or escalation_roles:
        yield from _escalation_role_issues(definition, escalation_roles)


def _check_escalation_user(
    node: GateNode | HumanInputNode, users: frozenset[str]
) -> Iterable[ValidationIssue]:
    """An ``escalate_to`` DID must be a registered user, never an agent.

    An escalation hands a human decision to whoever it names; naming an agent
    would let a model approve its own run (ASI09).
    """
    target = node.escalate_to
    if target is None or target.startswith(APPROVER_ROLE_PREFIX) or target in users:
        return
    yield ValidationIssue(
        node_id=node.id,
        field="escalate_to",
        error="escalate_to must name a registered user — this DID is not one",
        observed=target,
        admissible=tuple(sorted(users)),
    )


def check_gate_roles(
    definition: WorkflowSpec,
    declared_roles: frozenset[str],
    *,
    escalation_roles: frozenset[str] | None = None,
) -> tuple[ValidationIssue, ...]:
    """Every ``role:<name>`` a human step admits must be a role someone holds.

    A gate's and a human_input node's ``approvers`` are checked against
    ``declared_roles``; ``escalate_to`` against ``escalation_roles`` — the roles
    active human USERS hold (REQ-622) — or ``declared_roles`` when not given.
    Run start calls this with the live registry's rosters and refuses the run on
    any issue — unlike the authoring roster check, an EMPTY roster is not
    "skip" here: a step naming a role nobody holds could never be decided (an
    escalation to one would strand the run at its deadline), and a role that
    appears later must not silently widen who decides.
    """
    escalation = declared_roles if escalation_roles is None else escalation_roles
    return (
        *_approver_role_issues(definition, declared_roles),
        *_escalation_role_issues(definition, escalation),
    )


def _human_steps(definition: WorkflowSpec) -> Iterator[HumanStepSpec]:
    for node in definition.nodes:
        if node.kind in HUMAN_STEP_KINDS:
            yield cast(HumanStepSpec, node)


def _approver_role_issues(
    definition: WorkflowSpec, declared_roles: frozenset[str]
) -> Iterator[ValidationIssue]:
    admissible = _role_refs(declared_roles)
    for step in _human_steps(definition):
        for role in sorted(approver_roles(step.approvers) - declared_roles):
            yield _unknown_role(step.id, "approvers", f"role:{role}", admissible)


def _escalation_role_issues(
    definition: WorkflowSpec, user_roles: frozenset[str]
) -> Iterator[ValidationIssue]:
    """An escalation role must be held by at least one person, never only an agent."""
    admissible = _role_refs(user_roles)
    for step in _human_steps(definition):
        escalation = approver_roles((step.escalate_to,) if step.escalate_to else ())
        for role in sorted(escalation - user_roles):
            yield _unknown_role(step.id, "escalate_to", f"role:{role}", admissible)


def _role_refs(roles: frozenset[str]) -> tuple[str, ...]:
    return tuple(f"role:{r}" for r in sorted(roles))


def _unknown_role(
    node_id: str, field: str, observed: str, admissible: tuple[str, ...]
) -> ValidationIssue:
    return ValidationIssue(
        node_id=node_id,
        field=field,
        error="unknown role — no team member is registered with it",
        observed=observed,
        admissible=admissible,
    )


def _unknown(node_id: str, field: str, observed: str, roster: frozenset[str]) -> ValidationIssue:
    return ValidationIssue(
        node_id=node_id,
        field=field,
        error=f"unknown {field} — nothing by that name is registered",
        observed=observed,
        admissible=tuple(sorted(roster)),
    )


def _check_files(
    definition: WorkflowDefinition, bundle_root: Path | None, pending: frozenset[str]
) -> Iterable[ValidationIssue]:
    if bundle_root is None:
        return
    root = bundle_root.resolve()
    if definition.input_spec is not None:
        yield from _check_file(
            None, "input.schema", definition.input_spec.schema_ref, root, pending
        )
    for event in definition.events:
        yield from _check_file(None, _event_field(event.name), event.schema_ref, root, pending)
    for node in definition.nodes:
        if node.output_schema is not None:
            yield from _check_file(node.id, "output_schema", node.output_schema, root, pending)
        if isinstance(node, AgentNode) and node.prompt is not None:
            yield from _check_file(node.id, "prompt", node.prompt, root, pending)
        if isinstance(node, ScriptNode):
            yield from _check_file(node.id, "script", node.script, root, pending)


def _check_file(
    node_id: str | None, field: str, reference: str, root: Path, pending: frozenset[str]
) -> Iterable[ValidationIssue]:
    """A referenced file must resolve inside the bundle and actually be there.

    A path in ``pending`` is about to be written by the same transaction and
    counts as present; it is still confinement-checked.
    """
    resolved = confine(root, reference)
    if resolved is None:
        yield ValidationIssue(
            node_id=node_id,
            field=field,
            error="a file reference must be relative and must stay inside the bundle",
            observed=reference,
            admissible=("a relative path inside the workflow bundle",),
        )
    elif not resolved.is_file() and reference not in pending:
        yield ValidationIssue(
            node_id=node_id,
            field=field,
            error="the referenced file does not exist in the bundle",
            observed=reference,
            admissible=(f"a file present under {root.name}/",),
        )


def _event_field(name: str) -> str:
    return f"event.{name}.schema"


def _check_schema_contents(
    definition: WorkflowDefinition, bundle_root: Path | None
) -> Iterable[ValidationIssue]:
    """Rules about what a schema says, not just where it is.

    Only files already in the bundle are read. A missing or escaping file is
    reported once, by :func:`_check_files`; a file still pending in the same
    save has no bytes to read yet.
    """
    if bundle_root is None:
        return
    if definition.input_spec is not None:
        yield from _check_reserved_property(
            bundle_root, "input.schema", definition.input_spec.schema_ref
        )
    for event in definition.events:
        yield from _check_reserved_property(
            bundle_root, _event_field(event.name), event.schema_ref
        )
    for node in definition.nodes:
        if isinstance(node, GateNode | HumanInputNode) and node.on_timeout == "continue":
            yield from _check_continue_accepts_empty(bundle_root, node)


def _present_schema(
    bundle_root: Path, node_id: str | None, field: str, reference: str
) -> tuple[dict[str, Any] | None, ValidationIssue | None]:
    """Load a schema already in the bundle: ``(schema, None)``, ``(None, issue)``,
    or ``(None, None)`` when the file is absent (reported elsewhere)."""
    resolved = confine(bundle_root.resolve(), reference)
    if resolved is None or not resolved.is_file():
        return None, None
    try:
        return load_schema(bundle_root, reference), None
    except SchemaRefusedError as exc:
        return None, ValidationIssue(
            node_id=node_id,
            field=field,
            error=str(exc),
            observed=reference,
            admissible=("a Draft 2020-12 JSON Schema object of at most 64 KiB in the bundle",),
        )


def _check_reserved_property(
    bundle_root: Path, field: str, reference: str
) -> Iterable[ValidationIssue]:
    """No run-input schema may declare the key the runner records the trigger under."""
    schema, refused = _present_schema(bundle_root, None, field, reference)
    if refused is not None:
        yield refused
    if schema is None:
        return
    properties = schema.get("properties")
    if isinstance(properties, Mapping) and RESERVED_INPUT_PROPERTY in properties:
        yield ValidationIssue(
            field=field,
            error=(
                f"the schema declares {RESERVED_INPUT_PROPERTY!r}, which the runner reserves "
                "to record what started the run"
            ),
            observed=reference,
            admissible=(f"a schema without a {RESERVED_INPUT_PROPERTY!r} property",),
        )


def _check_continue_accepts_empty(
    bundle_root: Path, node: GateNode | HumanInputNode
) -> Iterable[ValidationIssue]:
    """``on_timeout = "continue"`` resolves with an empty payload, so ``{}`` must validate."""
    if node.output_schema is None:
        return
    schema, refused = _present_schema(bundle_root, node.id, "output_schema", node.output_schema)
    if refused is not None:
        yield refused
    if schema is None:
        return
    failures = check({}, schema)
    if failures:
        yield ValidationIssue(
            node_id=node.id,
            field="on_timeout",
            error=(
                "on_timeout = 'continue' resolves with an empty payload, but {} does not "
                f"validate against the output_schema: {failures[0].error}"
            ),
            observed=node.on_timeout,
            admissible=("fail", "escalate", "an output_schema that accepts {}"),
        )


def confine(root: Path, reference: str) -> Path | None:
    """Resolve ``reference`` under ``root``, or ``None`` if it escapes.

    Shared with the definition store, which hashes exactly the files this
    resolves — a manifest that could name a path outside the bundle would let a
    signature cover something the bundle does not own.
    """
    candidate = Path(reference)
    if candidate.is_absolute():
        return None
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        return None
    return resolved


# --- graph -------------------------------------------------------------------


class _Graph:
    """Adjacency over ``needs`` edges."""

    def __init__(self, definition: WorkflowDefinition) -> None:
        self.forward: dict[str, set[str]] = {node.id: set() for node in definition.nodes}
        for node in definition.nodes:
            for need in node.needs:
                self.forward[need].add(node.id)

    def descendants(self, start: str) -> frozenset[str]:
        """``start`` plus everything downstream of it over ``needs`` edges."""
        seen = {start}
        stack = [start]
        while stack:
            for successor in self.forward[stack.pop()]:
                if successor not in seen:
                    seen.add(successor)
                    stack.append(successor)
        return frozenset(seen)

    def ancestors(self, node_id: str) -> frozenset[str]:
        """Everything that must run before ``node_id`` over ``needs`` edges."""
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            for predecessor, successors in self.forward.items():
                if current in successors and predecessor not in seen:
                    seen.add(predecessor)
                    stack.append(predecessor)
        return frozenset(seen)


def _strongly_connected(adjacency: Mapping[str, set[str]]) -> list[frozenset[str]]:
    """Tarjan's SCCs, iterative so a long chain cannot exhaust the stack."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[frozenset[str]] = []
    counter = 0

    for root in adjacency:
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [(root, list(adjacency[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                successor = pending.pop()
                if successor not in index:
                    index[successor] = low[successor] = counter
                    counter += 1
                    stack.append(successor)
                    on_stack.add(successor)
                    work.append((successor, list(adjacency[successor])))
                elif successor in on_stack:
                    low[node] = min(low[node], index[successor])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                components.append(_pop_component(stack, on_stack, node))
    return components


def _pop_component(stack: list[str], on_stack: set[str], root: str) -> frozenset[str]:
    component: set[str] = set()
    while True:
        member = stack.pop()
        on_stack.discard(member)
        component.add(member)
        if member == root:
            return frozenset(component)


# --- cycles ------------------------------------------------------------------


def _check_cycles(definition: WorkflowDefinition, graph: _Graph) -> Iterable[ValidationIssue]:
    """A workflow is a DAG: any cycle, however small, is refused."""
    del definition
    for component in _strongly_connected(graph.forward):
        if (
            len(component) == 1
            and next(iter(component)) not in graph.forward[next(iter(component))]
        ):
            continue
        members = sorted(component)
        yield ValidationIssue(
            node_id=members[0],
            field="needs",
            error=f"cycle through {', '.join(members)} - a workflow must be a DAG",
            observed=members,
            admissible=("remove one of the needs edges that closes the cycle",),
        )


# --- exclusive routes --------------------------------------------------------


def _check_exclusive_needs(
    definition: WorkflowDefinition, graph: _Graph
) -> Iterable[ValidationIssue]:
    """Reject the deadlock: needs spanning exclusive router routes."""
    exclusive = _exclusive_regions(definition, graph)
    for node in definition.nodes:
        if len(node.needs) < 2:
            continue
        if any(_spans_two_routes(node.needs, regions) for regions in exclusive):
            yield ValidationIssue(
                node_id=node.id,
                field="needs",
                error=(
                    "these needs sit on mutually exclusive routes of one router, so this node "
                    "can never become ready; fan-in after a router is not supported"
                ),
                observed=list(node.needs),
                admissible=("continue each route on its own branch",),
            )


def _exclusive_regions(
    definition: WorkflowDefinition, graph: _Graph
) -> list[list[frozenset[str]]]:
    """Per router, the set of nodes reachable *only* through each of its routes."""
    regions: list[list[frozenset[str]]] = []
    for node in definition.nodes:
        if not isinstance(node, RouterNode) or len(node.routes) < 2:
            continue
        reachable = [graph.descendants(route.to) for route in node.routes]
        regions.append(
            [
                frozenset(members - set().union(*(reachable[:i] + reachable[i + 1 :])))
                for i, members in enumerate(reachable)
            ]
        )
    return regions


def _spans_two_routes(needs: tuple[str, ...], regions: list[frozenset[str]]) -> bool:
    touched = {i for i, region in enumerate(regions) if region & set(needs)}
    return len(touched) > 1


# --- output references -------------------------------------------------------


def _check_output_references(
    definition: WorkflowDefinition, graph: _Graph
) -> Iterable[ValidationIssue]:
    """A node may only read output of nodes it is guaranteed to run after."""
    exclusive = _exclusive_regions(definition, graph)
    ids = set(definition.node_ids)
    for node in definition.nodes:
        ancestors = graph.ancestors(node.id)
        for field, referenced in _referenced_node_ids(node):
            yield from _check_reference(node, field, referenced, ancestors, ids, exclusive)
        if isinstance(node, ToolNode):
            yield from _check_arg_strings(node)


_PROSE_REFERENCE = re.compile(r"\$nodes\.([A-Za-z][A-Za-z0-9_-]*)\.output\b")


def _check_prompt_references(
    definition: WorkflowDefinition, graph: _Graph, bundle_root: Path | None
) -> Iterable[ValidationIssue]:
    """Prose may mention only the output of nodes it is guaranteed to run after.

    A prompt that cites ``$nodes.X.output`` for a node that is not an ancestor
    describes data the node will never be handed; it is caught here, not by a
    model improvising at run time. A prompt that is not on disk yet is skipped
    (the file check reports it).
    """
    if bundle_root is None:
        return
    root = bundle_root.resolve()
    exclusive = _exclusive_regions(definition, graph)
    ids = set(definition.node_ids)
    for node in definition.nodes:
        if not isinstance(node, AgentNode) or node.prompt is None:
            continue
        path = confine(root, node.prompt)
        if path is None or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        ancestors = graph.ancestors(node.id)
        for referenced in sorted(set(_PROSE_REFERENCE.findall(text))):
            yield from _check_reference(node, "prompt", referenced, ancestors, ids, exclusive)


def _referenced_node_ids(node: WorkflowNode) -> Iterable[tuple[str, str]]:
    """Every ``(field, referenced_node_id)`` pair this node's wiring reads."""
    if node.when is not None:
        for path in _safe_paths(node.when):
            if path.node_id is not None:
                yield ("when", path.node_id)
    if isinstance(node, RouterNode):
        for route in node.routes:
            for path in _safe_paths(route.when):
                if path.node_id is not None:
                    yield ("routes", path.node_id)
    if isinstance(node, ToolNode):
        for reference in _safe_references(node.args):
            if reference is not None:
                yield ("args", reference)


def _safe_paths(expression: str | None) -> tuple[PathRef, ...]:
    """Paths in a predicate already known to parse (a bad one is reported once)."""
    if expression is None:
        return ()
    try:
        return paths_in(parse_predicate(expression))
    except PredicateError:
        return ()


def _safe_references(args: Mapping[str, Any]) -> tuple[str | None, ...]:
    try:
        return tuple(reference.node_id for reference in references_in(args))
    except UnresolvableReferenceError:
        return ()


def _check_reference(
    node: WorkflowNode,
    field: str,
    referenced: str,
    ancestors: frozenset[str],
    ids: set[str],
    exclusive: list[list[frozenset[str]]],
) -> Iterable[ValidationIssue]:
    if referenced not in ids:
        yield ValidationIssue(
            node_id=node.id,
            field=field,
            error="the reference names a node that does not exist",
            observed=referenced,
            admissible=tuple(sorted(ids - {node.id})),
        )
    elif referenced not in ancestors:
        yield ValidationIssue(
            node_id=node.id,
            field=field,
            error=_unreachable_reason(node.id, referenced, exclusive),
            observed=referenced,
            admissible=tuple(sorted(ancestors)) or ("a node this one depends on",),
        )


def _unreachable_reason(
    node_id: str, referenced: str, exclusive: list[list[frozenset[str]]]
) -> str:
    for regions in exclusive:
        here = {i for i, region in enumerate(regions) if node_id in region}
        there = {i for i, region in enumerate(regions) if referenced in region}
        if here and there and not here & there:
            return (
                f"this node cannot co-occur with {referenced!r}: they sit on mutually exclusive "
                f"routes of one router, so the reference can never bind"
            )
    return (
        f"{referenced!r} is not a dependency of this node, so its output may not exist when "
        f"this node runs"
    )


def _check_arg_strings(node: ToolNode) -> Iterable[ValidationIssue]:
    """Report every argument string that is reference-shaped but unusable.

    Two distinct mistakes, each reported once: a reference spliced into a
    string, and a string that means to be a reference but does not parse.
    """
    embedded = embedded_reference_strings(node.args)
    for offender in embedded:
        yield ValidationIssue(
            node_id=node.id,
            field="args",
            error=(
                "an argument embeds a reference inside a string; upstream output binds as a "
                "typed value, never as substituted text"
            ),
            observed=offender,
            admissible=("the reference as the whole argument value",),
        )
    for offender in malformed_reference_strings(node.args):
        if offender in embedded:
            continue
        yield ValidationIssue(
            node_id=node.id,
            field="args",
            error="the argument looks like a reference but is not a well-formed one",
            observed=offender,
            admissible=(
                "$nodes.<node_id>.output.<field>",
                "$input.<field>",
            ),
        )


__all__ = [
    "RESERVED_INPUT_PROPERTY",
    "KnownReferences",
    "approver_roles",
    "check_gate_roles",
    "confine",
    "validate_definition",
]
