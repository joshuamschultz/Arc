"""H-016 — MemoryOperator.graph: windowed neighborhood view of the associative graph.

The graph viewer (arcui Knowledge tab) consumes ONLY ``MemoryOperator.graph`` — no
consumer runs SQL against ``edges`` directly (same COMP-001 seam rule as the rest of
the operator facade). Every fixture entity here is written through
``SemanticStore.write_fact`` (the real capture path), never hand-inserted SQL, so
these tests exercise the same graph production writes.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.operator import GraphEdge, GraphNode, MemoryGraph, MemoryOperator
from arcmemory.stores.semantic import SemanticStore

_DID = "did:arc:graph-agent"


def _operator(workspace: Path) -> MemoryOperator:
    return MemoryOperator(workspace, _DID)


def _semantic(workspace: Path) -> SemanticStore:
    return SemanticStore(workspace, WeightedGraph(MemoryDB(workspace)), scope=_DID)


def _link(workspace: Path, a: str, b: str, *, classification_a: str = "unclassified") -> None:
    """Write two entities plus a wiki-link edge between them, real capture path."""
    store = _semantic(workspace)
    store.write_fact(a, "role", "member", classification=classification_a)
    store.write_fact(a, "links-to", f"[[{b}]]", classification=classification_a)


# -- shape: nodes + edges with metadata ---------------------------------------


def test_graph_returns_nodes_and_edges_with_metadata(workspace: Path) -> None:
    _link(workspace, "alice", "bob")
    graph = _operator(workspace).graph(node="alice", hops=1)

    assert isinstance(graph, MemoryGraph)
    node_ids = {n.id for n in graph.nodes}
    assert node_ids == {"alice", "bob"}

    alice = next(n for n in graph.nodes if n.id == "alice")
    assert isinstance(alice, GraphNode)
    assert alice.node_type == "entity"
    assert alice.classification == "unclassified"
    assert alice.metadata["entity_type"]  # decorated metadata, not a bare id

    assert len(graph.edges) == 1
    edge = graph.edges[0]
    assert isinstance(edge, GraphEdge)
    assert {edge.src, edge.dst} == {"alice", "bob"}
    assert edge.kind == "link"
    assert edge.weight > 0


def test_graph_with_no_node_returns_deterministic_whole_scope_window(workspace: Path) -> None:
    _link(workspace, "alice", "bob")
    _link(workspace, "carol", "dave")
    first = _operator(workspace).graph()
    second = _operator(workspace).graph()

    assert {n.id for n in first.nodes} == {"alice", "bob", "carol", "dave"}
    # Deterministic (sorted-id) window — repeat calls return the identical shape.
    assert [n.id for n in first.nodes] == [n.id for n in second.nodes]


# -- server-side caps: hops AND node count ------------------------------------


def test_graph_hops_cap_limits_bfs_radius(workspace: Path) -> None:
    # a -> b -> c -> d chain (each hop a separate wiki-link).
    _link(workspace, "a", "b")
    _link(workspace, "b", "c")
    _link(workspace, "c", "d")

    one_hop = _operator(workspace).graph(node="a", hops=1)
    assert {n.id for n in one_hop.nodes} == {"a", "b"}

    two_hop = _operator(workspace).graph(node="a", hops=2)
    assert {n.id for n in two_hop.nodes} == {"a", "b", "c"}


def test_graph_hops_param_is_clamped_to_config_ceiling(workspace: Path) -> None:
    # Chain longer than the tiered max_hops (default 3): a-b-c-d-e-f-g.
    letters = ["a", "b", "c", "d", "e", "f", "g"]
    for src, dst in pairwise(letters):
        _link(workspace, src, dst)

    op = _operator(workspace)
    huge_hops = op.graph(node="a", hops=10_000)
    capped_hops = op.graph(node="a", hops=3)  # the tiered MemoryConfig.max_hops default

    assert {n.id for n in huge_hops.nodes} == {n.id for n in capped_hops.nodes}
    assert "g" not in {n.id for n in huge_hops.nodes}  # never reaches the whole 6-hop chain


def test_graph_node_count_is_capped(workspace: Path) -> None:
    for i in range(10):
        _link(workspace, f"n{i}", f"m{i}")
    graph = _operator(workspace).graph(max_nodes=3)
    assert len(graph.nodes) <= 3


def test_graph_max_nodes_cannot_exceed_hard_ceiling(workspace: Path) -> None:
    for i in range(5):
        _link(workspace, f"n{i}", f"m{i}")
    # A caller asking for an absurd window still gets the neighborhood cap, not a dump.
    graph = _operator(workspace).graph(max_nodes=10_000_000)
    assert len(graph.nodes) == 10  # every node in this tiny fixture, not an overflow


# -- classification gating: drop + NO leak of the hidden node ----------------


def test_graph_drops_node_above_clearance_with_no_leak(workspace: Path) -> None:
    _link(workspace, "alice", "secret-project", classification_a="unclassified")
    # Raise the far endpoint's own classification above alice's.
    _semantic(workspace).write_fact("secret-project", "status", "active", classification="secret")

    graph = _operator(workspace).graph(node="alice", hops=1, clearance="unclassified")

    node_ids = {n.id for n in graph.nodes}
    assert node_ids == {"alice"}  # the secret node is gone entirely
    assert graph.edges == []  # its edge is gone too — no far-endpoint leak via a dangling edge

    # No trace anywhere in the serialized payload: not in an id, not in metadata,
    # not as a count/degree hint.
    dumped = graph.model_dump_json()
    assert "secret-project" not in dumped
    assert len(graph.nodes) == 1
    assert len(graph.edges) == 0


def test_graph_shows_node_when_clearance_dominates(workspace: Path) -> None:
    _link(workspace, "alice", "secret-project", classification_a="unclassified")
    _semantic(workspace).write_fact("secret-project", "status", "active", classification="secret")

    graph = _operator(workspace).graph(node="alice", hops=1, clearance="secret")

    assert {n.id for n in graph.nodes} == {"alice", "secret-project"}
    assert len(graph.edges) == 1


def test_graph_center_node_itself_gated(workspace: Path) -> None:
    """Even the requested center node is subject to the same gate — asking for a
    node above your clearance returns an empty graph, not the node anyway."""
    _semantic(workspace).write_fact("classified-root", "status", "x", classification="secret")

    graph = _operator(workspace).graph(node="classified-root", hops=1, clearance="unclassified")
    assert graph.nodes == []
    assert graph.edges == []


# -- cue nodes (no backing file) ----------------------------------------------


def test_graph_bare_cue_reads_as_unclassified_entity_type_cue(workspace: Path) -> None:
    """An edge endpoint with no backing entity file is a bare cue — unclassified,
    dominated by every clearance, never invented a classification for it."""
    store = _semantic(workspace)
    # write_fact("alice", ...) creates the alice entity file; the wiki-link target
    # "ghost" never gets its own write_fact, so it has no backing file (a cue).
    store.write_fact("alice", "mentions", "[[ghost]]")

    graph = _operator(workspace).graph(node="alice", hops=1)
    ghost = next(n for n in graph.nodes if n.id == "ghost")
    assert ghost.node_type == "cue"
    assert ghost.classification == "unclassified"
