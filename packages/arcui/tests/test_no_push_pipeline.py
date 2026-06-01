"""Architecture test — the live push pipeline is gone (SPEC-026 FR-5).

arcui is a read-only consumer of the durable arcstore record. These checks make
the teardown structural: a future re-introduction of a push wire (an event
buffer, a subscription broadcaster, an audit-sink bridge, a `/ws` telemetry
feed) trips here instead of silently resurrecting the miss/no-update bug class
this spec exists to kill (AC-5.1, AC-5.4, task 4.15).
"""

from __future__ import annotations

import importlib

import pytest

_SRC = __import__("arcui").__path__[0]


# Modules deleted in the teardown — importing any of them must fail.
_DELETED_MODULES = [
    "arcui.bridge",
    "arcui.aggregator",
    "arcui.event_buffer",
    "arcui.subscription",
    "arcui.connection",
    "arcui.reporter",
    "arcui.file_change_bridge",
    "arcui.team_chat_bridge",
    "arcui.transport",
    "arcui.transport_ws",
    "arcui.federated_store",
    "arcui.routes.ws",
    "arcui.routes.dashboard_ws",
    "arcui.routes.agent_ws",
    "arcui.routes.schedules",
]


@pytest.mark.parametrize("module", _DELETED_MODULES)
def test_push_pipeline_module_is_gone(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_no_push_symbol_references_in_src() -> None:
    """No arcui source *code* may reference a deleted push primitive.

    AST-based so it ignores comments/docstrings (which legitimately say "the
    push pipeline is gone") — it flags only real identifier uses: a dangling
    name, attribute access, or a half-resurrected wire.
    """
    import ast
    from pathlib import Path

    banned = {
        "UIBridgeSink",
        "RollingAggregator",
        "EventBuffer",
        "SubscriptionManager",
        "ConnectionManager",
        "FederatedTraceStore",
        "dashboard_bus",
        "event_buffer",
        "subscription_manager",
        "connection_manager",
    }
    offenders: dict[str, set[str]] = {}
    for path in Path(_SRC).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in banned:
                used.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in banned:
                used.add(node.attr)
        if used:
            offenders[path.name] = used
    assert not offenders, f"deleted push primitives still referenced in arcui src: {offenders}"


def test_observe_run_routes_are_pull_only() -> None:
    """SPEC-028 task 4.9 — the new tool/lineage/identity routes are synchronous
    reads (``Route``), never a ``WebSocketRoute`` or any push wire."""
    from starlette.routing import Route

    from arcui.routes import observe_run

    assert observe_run.routes, "observe_run must register read routes"
    assert all(isinstance(r, Route) for r in observe_run.routes), (
        "SPEC-028 surfaces must be pull-only Route handlers (D-007)"
    )


def test_arcui_is_not_an_emit_subscriber() -> None:
    """arcui must not register itself as a sink/subscriber of arctrust.emit().

    No arcui source imports ``emit`` from arctrust — the UI reads the durable
    record through ``arcstore``, never the live emission path (AC-5.4).
    """
    from pathlib import Path

    offenders = []
    for path in Path(_SRC).rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "from arctrust.audit import" in text and "emit" in text:
            offenders.append(path.name)
        if "audit.emit(" in text:
            offenders.append(path.name)
    assert not offenders, f"arcui must not call arctrust.emit(): {offenders}"
