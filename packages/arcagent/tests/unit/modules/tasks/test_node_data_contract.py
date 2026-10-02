"""P14-B step 6 — a node always says what it was handed, and what failed upstream."""

from __future__ import annotations

from arcagent.modules.tasks.node_execution import WorkflowNode, render_node_section


def _node(**fields: object) -> WorkflowNode:
    return WorkflowNode(workflow_id="w", run_id="r", node_id="n", **fields)  # type: ignore[arg-type]


def test_upstream_block_is_always_rendered_with_an_explicit_none() -> None:
    section = render_node_section(_node())

    assert "### Upstream outputs" in section
    assert "(none)" in section


def test_upstream_block_lists_outputs_when_present() -> None:
    section = render_node_section(_node(upstream={"collect": {"x": 1}}))

    assert "`collect`" in section
    assert "(none)" not in section


def test_failed_upstream_is_named_with_its_reason() -> None:
    section = render_node_section(_node(upstream_failed={"fetch": "timeout after 30s"}))

    assert "### Upstream failures" in section
    assert "`fetch`" in section and "timeout after 30s" in section
