"""COMP-002 — whole-graph static validation (SPEC-061 REQ-219/REQ-220).

Every authoring surface — the agent's builder tools, a hand edit in an IDE, the
dashboard editor — runs this one validator over the *whole* graph before any
write. Node-by-node validation while writing is what lets a half-written graph
reach disk, so validation is always in-memory and always complete.

The checks that matter most are the ones comparable engines defer to runtime
and then debug forever:

* **The join deadlock.** A node whose ``needs`` span mutually exclusive routes
  of one router waits forever under the default ``join = "all"``, because only
  one of those branches will ever run. The design's own example graph shipped
  with this bug. It is rejected here, statically, with ``join = "any"`` named
  as the fix.
* **Undeclared cycles.** Cycles are legal but must be *declared*: every
  strongly-connected component larger than one node must be entered by exactly
  one ``loop_back_to``, all its members share that one counter, and the
  back-edge must carry ``max_iterations``. Nested and overlapping loops are
  refused in v1 rather than given ambiguous counter semantics.
* **Statically unsatisfiable output references.** A node reading
  ``$nodes.X.output.*`` where X is not a transitive dependency — or sits on a
  branch the reader can never co-occur with — can never bind that value.

Every rejection is a :class:`~arcteam.workflow.errors.ValidationIssue` naming
the node, the field, the observed value, and the admissible alternatives, and
the whole list comes back at once so a repair pass sees every problem.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from arcteam.workflow.errors import (
    PredicateError,
    UnresolvableReferenceError,
    ValidationIssue,
)
from arcteam.workflow.models import (
    MAX_DEFINITION_BYTES,
    MAX_NODES,
    AgentNode,
    RouterNode,
    ScriptNode,
    ToolNode,
    WorkflowDefinition,
    WorkflowNode,
)
from arcteam.workflow.predicates import PathRef, parse_predicate, paths_in
from arcteam.workflow.resolver import embedded_reference_strings, references_in


class KnownReferences(BaseModel):
    """The roster a definition's references are checked against.

    An empty set means "nothing known of this kind", which is why the caller
    passes ``known=None`` to skip roster checking entirely rather than passing
    empty sets and getting everything rejected.
    """

    model_config = ConfigDict(frozen=True)

    agents: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()
    skills: frozenset[str] = frozenset()


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
    if structural:
        return tuple(issues)

    graph = _Graph(definition)
    cycles = tuple(_check_cycles(definition, graph))
    issues += cycles
    if cycles:
        return tuple(issues)

    issues += _check_joins(definition, graph)
    issues += _check_output_references(definition, graph)
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
        if node.loop_back_to is not None and node.loop_back_to not in ids:
            yield ValidationIssue(
                node_id=node.id,
                field="loop_back_to",
                error="loop_back_to names a node that does not exist",
                observed=node.loop_back_to,
                admissible=tuple(sorted(ids)),
            )
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
        if node.loop_back_to is not None and node.max_iterations is None:
            yield ValidationIssue(
                node_id=node.id,
                field="max_iterations",
                error="a declared loop must carry a hard iteration bound",
                observed=None,
                admissible=("max_iterations = 1..100",),
            )
        if node.loop_back_to is None and node.max_iterations is not None:
            yield ValidationIssue(
                node_id=node.id,
                field="max_iterations",
                error="max_iterations only means something on a node declaring loop_back_to",
                observed=node.max_iterations,
                admissible=("remove max_iterations", "add loop_back_to = <ancestor node id>"),
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


def _check_known(
    definition: WorkflowDefinition, known: KnownReferences | None
) -> Iterable[ValidationIssue]:
    if known is None:
        return
    if definition.owner not in known.agents:
        yield ValidationIssue(
            field="owner",
            error="the workflow owner is not a registered agent",
            observed=definition.owner,
            admissible=tuple(sorted(known.agents)),
        )
    for node in definition.nodes:
        if node.agent is not None and node.agent not in known.agents:
            yield _unknown(node.id, "agent", node.agent, known.agents)
        if isinstance(node, ToolNode) and node.tool not in known.tools:
            yield _unknown(node.id, "tool", node.tool, known.tools)
        if isinstance(node, AgentNode) and node.skill is not None:
            if node.skill not in known.skills:
                yield _unknown(node.id, "skill", node.skill, known.skills)


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
    """Adjacency over ``needs`` edges, plus declared back-edges."""

    def __init__(self, definition: WorkflowDefinition) -> None:
        self.forward: dict[str, set[str]] = {node.id: set() for node in definition.nodes}
        self.with_back_edges: dict[str, set[str]] = {node.id: set() for node in definition.nodes}
        for node in definition.nodes:
            for need in node.needs:
                self.forward[need].add(node.id)
                self.with_back_edges[need].add(node.id)
            if node.loop_back_to is not None:
                self.with_back_edges[node.id].add(node.loop_back_to)

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


# --- cycles and loops --------------------------------------------------------


def _check_cycles(definition: WorkflowDefinition, graph: _Graph) -> Iterable[ValidationIssue]:
    """Every cycle must be one declared, bounded loop. Nothing else is legal."""
    in_a_loop: set[str] = set()
    for component in _strongly_connected(graph.with_back_edges):
        if len(component) == 1 and not _has_self_edge(graph, component):
            continue
        back_edges = [
            node
            for node in definition.nodes
            if node.id in component and node.loop_back_to in component
        ]
        if len(back_edges) != 1:
            yield _cycle_issue(component, back_edges)
            continue
        in_a_loop.update(component)
    yield from _check_stray_back_edges(definition, in_a_loop)


def _has_self_edge(graph: _Graph, component: frozenset[str]) -> bool:
    node_id = next(iter(component))
    return node_id in graph.with_back_edges[node_id]


def _cycle_issue(component: frozenset[str], back_edges: list[WorkflowNode]) -> ValidationIssue:
    members = sorted(component)
    if not back_edges:
        return ValidationIssue(
            node_id=members[0],
            field="needs",
            error=(
                f"undeclared cycle through {', '.join(members)} — a cycle is legal only when "
                f"one node in it declares loop_back_to with max_iterations"
            ),
            observed=members,
            admissible=(f"loop_back_to = one of {members}", "max_iterations = 1..100"),
        )
    return ValidationIssue(
        node_id=sorted(node.id for node in back_edges)[0],
        field="loop_back_to",
        error=(
            f"the cycle through {', '.join(members)} must be entered by exactly one declared "
            f"back-edge so all its members share one counter; nested and overlapping loops "
            f"are not supported"
        ),
        observed=sorted(node.id for node in back_edges),
        admissible=("exactly one node in the cycle declaring loop_back_to",),
    )


def _check_stray_back_edges(
    definition: WorkflowDefinition, in_a_loop: set[str]
) -> Iterable[ValidationIssue]:
    """A back-edge that forms no cycle is a mislabelled forward edge."""
    for node in definition.nodes:
        if node.loop_back_to is not None and node.id not in in_a_loop:
            yield ValidationIssue(
                node_id=node.id,
                field="loop_back_to",
                error=(
                    "loop_back_to must target an ancestor of this node so the back-edge closes "
                    "a real loop"
                ),
                observed=node.loop_back_to,
                admissible=("a node this one transitively depends on",),
            )


# --- joins -------------------------------------------------------------------


def _check_joins(definition: WorkflowDefinition, graph: _Graph) -> Iterable[ValidationIssue]:
    """Reject the deadlock: needs spanning exclusive router routes under join=all."""
    exclusive = _exclusive_regions(definition, graph)
    for node in definition.nodes:
        if node.join == "any" or len(node.needs) < 2:
            continue
        if any(_spans_two_routes(node.needs, regions) for regions in exclusive):
            yield ValidationIssue(
                node_id=node.id,
                field="join",
                error=(
                    "these needs sit on mutually exclusive routes of one router, so under "
                    'join = "all" this node can never become ready'
                ),
                observed=node.join,
                admissible=("any",),
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
            yield from _check_embedded(node)


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


def _check_embedded(node: ToolNode) -> Iterable[ValidationIssue]:
    """Refuse a reference spliced into a string before it can ever be run."""
    for offender in embedded_reference_strings(node.args):
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


__all__ = ["KnownReferences", "confine", "validate_definition"]
