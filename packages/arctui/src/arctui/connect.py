"""SPEC-064 T-030 — the connector work behind ``/connect``, with no Textual in it.

The TUI needs its own door to connector setup because ``arc connector add`` asks
for credentials with ``getpass``, which reads the controlling terminal — the one
Textual has taken over (D-586). What it must *not* have is its own idea of how a
connection is made: the world resolution, the audit chain's lifetime, the
ordering, the signature gate, the probe, and the rollback all live behind
:class:`arcagent.connections.Connections`, which the CLI and the web drive too
(D-587).

So this module is two things and nothing more: the chain opener the TUI hands to
that seam, and the lines a screen shows. Keeping it free of widgets is what lets
the flow be read and tested as a sequence of calls rather than as a sequence of
key presses.

**A credential passes through and is never kept.** Values arrive from the screen's
masked inputs, go straight to :meth:`~arcagent.connections.Connections.install`,
and appear in no return value, no log line, and no exception message raised here.
"""

from __future__ import annotations

from pathlib import Path

from arcagent.connections import (
    AuditChain,
    ClosableSink,
    Connections,
    ConnectionWorld,
    ConnectorPlan,
    InstallReport,
    resolve_world,
)


def open_connections(agent_dir: Path) -> Connections:
    """Resolve the attached agent's connector world and bind it to the audit chain.

    Args:
        agent_dir: The agent directory holding ``arcagent.toml`` — the one arctui
            already resolved from the roster to decide what it attached to.

    Returns:
        The seam every screen drives.

    Raises:
        ExtensionError: No agent config, an unparseable one, or no DID to record
            as the actor on the credential write. ``.message`` names which.
    """
    world = resolve_world(agent_dir)
    return Connections(world, audit=AuditChain.opened_by(lambda: _worm_sink(world)))


def _worm_sink(world: ConnectionWorld) -> ClosableSink:
    """Open the operator-signed WORM chain for one action.

    Each action opens and closes its own rather than the screen holding one for as
    long as it is on screen: the sink keeps an exclusive ``flock`` for its lifetime,
    so one held across a modal would lock every later writer out of the chain.
    The seam closes it — this only says where it lives.
    """
    from arccli.commands.operator import operator_worm_sink

    return operator_worm_sink(world.arc_dir, world.data_dir)


def plan_summary(plan: ConnectorPlan, env_file: Path) -> tuple[str, ...]:
    """What is about to happen, in the order it will happen — shown before any input."""
    lines = [
        f"{plan.extension} → instance '{plan.instance}'",
        f"  approval mode  : {plan.approval_mode}",
        f"  credentials in : {env_file}  (owner-only, never the config)",
    ]
    if plan.secrets:
        asked = ", ".join(declared.name for declared in plan.secrets)
        lines.append(f"  will ask for   : {asked}")
    else:
        lines.append("  will ask for   : (nothing — this bundle declares no credentials)")
    declared_tools = ", ".join(tool.name for tool in plan.manifest.tools.declared)
    lines.append(f"  tools declared : {declared_tools or '(none until the probe answers)'}")
    return tuple(lines)


def host_refusal(plan: ConnectorPlan) -> tuple[str, ...]:
    """The instruction for a prerequisite this machine lacks. The TUI never installs it."""
    return (
        f"{plan.extension} needs prerequisites this machine lacks. Nothing was installed.",
        *(f"  {verdict.name}: {verdict.instruction}" for verdict in plan.unsatisfied_host),
    )


def install_summary(report: InstallReport, connections: Connections) -> tuple[str, ...]:
    """What the install produced. Coordinates only — never a value the operator typed."""
    world = connections.world
    return (
        f"Connected {report.extension} as instance '{report.instance}'.",
        f"  agent          : {world.agent}",
        f"  credentials in : {world.env_file}  (owner-only)",
        f"  probe          : {report.detail or '(reachable)'}",
        f"  tools          : {', '.join(report.tools) or '(none served)'}",
    )


__all__ = [
    "host_refusal",
    "install_summary",
    "open_connections",
    "plan_summary",
]
