"""SPEC-017 Phase 8 — CLI groups for policy / completion / schedule.

These Click groups are thin wrappers around core APIs that favor
scriptability (JSON output) so CI pipelines can consume them.

Extracted from arccli.agent (legacy Click module) to keep the Click
dependency contained and allow the main agent dispatch layer to be
pure argparse.

Public exports: policy_group, completion_group, schedule_group
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from arccli.formatting import click_echo

# ---------------------------------------------------------------------------
# policy group
# ---------------------------------------------------------------------------


@click.group("policy")
def policy_group() -> None:
    """Inspect the tool-policy pipeline."""


@policy_group.command("layers")
@click.option(
    "--tier",
    type=click.Choice(["federal", "enterprise", "personal"]),
    default="personal",
    show_default=True,
)
def policy_layers(tier: str) -> None:
    """List layers active for the given tier."""
    from arcagent.core.tool_policy import build_pipeline

    pipeline = build_pipeline(tier=tier)  # type: ignore[arg-type]
    names = [layer.name for layer in pipeline.layers]
    click_echo(json.dumps({"tier": tier, "layers": names}, indent=2))


@policy_group.command("evaluate")
@click.option(
    "--tier",
    type=click.Choice(["federal", "enterprise", "personal"]),
    default="personal",
)
@click.option("--tool", "tool_name", required=True, help="Tool name to evaluate.")
@click.option("--agent-did", "agent_did", default="did:arc:cli")
@click.option(
    "--classification",
    default="unclassified",
    show_default=True,
)
def policy_evaluate(
    tier: str, tool_name: str, agent_did: str, classification: str
) -> None:
    """Dry-run evaluate a tool call; print the decision as JSON."""
    import asyncio as _asyncio

    from arcagent.core.tool_policy import (
        PolicyContext,
        ToolCall,
        build_pipeline,
    )

    pipeline = build_pipeline(tier=tier)  # type: ignore[arg-type]
    call = ToolCall(
        tool_name=tool_name,
        arguments={},
        agent_did=agent_did,
        session_id="cli",
        classification=classification,
    )
    ctx = PolicyContext(tier=tier, policy_version="v1", bundle_age_seconds=0.0)  # type: ignore[arg-type]
    decision = _asyncio.run(pipeline.evaluate(call, ctx))
    click_echo(json.dumps(decision.model_dump(), indent=2))


# ---------------------------------------------------------------------------
# completion group
# ---------------------------------------------------------------------------


@click.group("completion")
def completion_group() -> None:
    """Inspect ``task_complete`` history (reads audit log)."""


@completion_group.command("history")
@click.option(
    "--path", default=".", help="Agent workspace path.", show_default=True
)
@click.option("--limit", type=int, default=20, show_default=True)
def completion_history(path: str, limit: int) -> None:
    """Print the most recent ``task_complete`` events from the audit log.

    Reads the workspace's ``audit/`` directory and filters for
    ``loop.completed`` events. Output is JSON for easy piping.
    """
    from pathlib import Path as _Path

    agent_dir = _Path(path).resolve()
    audit_dir = agent_dir / "workspace" / "audit"
    if not audit_dir.exists():
        click_echo(json.dumps({"events": []}))
        return
    events: list[dict[str, Any]] = []
    for log_file in sorted(audit_dir.glob("*.jsonl"), reverse=True):
        for line in reversed(log_file.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("event_type") == "loop.completed":
                events.append(entry)
                if len(events) >= limit:
                    break
        if len(events) >= limit:
            break
    click_echo(json.dumps({"events": events}, indent=2))


# ---------------------------------------------------------------------------
# schedule group
# ---------------------------------------------------------------------------


@click.group("schedule")
def schedule_group() -> None:
    """Manage proactive schedules (replaces legacy scheduler CLI)."""


@schedule_group.command("list")
@click.option(
    "--path", default=".", help="Agent workspace path.", show_default=True
)
def schedule_list(path: str) -> None:
    """List persisted schedules from the workspace state file.

    Reads ``workspace/proactive/schedules.json`` — schedules are
    written there by the engine on every mutation. Safe to run while
    the agent is offline.
    """
    from pathlib import Path as _Path

    agent_dir = _Path(path).resolve()
    state_file = agent_dir / "workspace" / "proactive" / "schedules.json"
    if not state_file.exists():
        click_echo(json.dumps({"schedules": []}))
        return
    click_echo(state_file.read_text(encoding="utf-8"))


@schedule_group.command("migrate")
@click.option(
    "--path", default=".", help="Agent workspace path.", show_default=True
)
@click.option(
    "--dry-run", is_flag=True, help="Print migration plan without writing."
)
def schedule_migrate(path: str, dry_run: bool) -> None:
    """One-time migration from legacy scheduler state to the proactive engine.

    SPEC-017 R-040 deleted ``modules/scheduler/`` and its persisted
    state format. Deployments upgrading from arc-agent < 0.3.0 must
    run this command once to convert their old schedule definitions.

    Reads ``workspace/scheduler/`` (legacy JSONL state) and emits
    ``workspace/proactive/schedules.json`` in the new format.
    """
    from pathlib import Path as _Path

    agent_dir = _Path(path).resolve()
    legacy_dir = agent_dir / "workspace" / "scheduler"
    target_dir = agent_dir / "workspace" / "proactive"
    target_file = target_dir / "schedules.json"

    if not legacy_dir.exists():
        click_echo(
            json.dumps(
                {
                    "status": "no-op",
                    "reason": "legacy scheduler/ directory not present",
                    "legacy_dir": str(legacy_dir),
                }
            )
        )
        return

    migrated: list[dict[str, Any]] = []
    for state_file in sorted(legacy_dir.glob("*.jsonl")):
        for line in state_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            migrated.append(
                {
                    "id": entry["id"],
                    "interval_seconds": entry.get("interval_seconds", 60),
                    "kind": entry.get("kind", "cron"),
                    "metadata": {
                        "migrated_from": "scheduler",
                        "source_file": state_file.name,
                        "original_cron": entry.get("cron_expression", ""),
                    },
                }
            )

    plan: dict[str, Any] = {
        "status": "dry-run" if dry_run else "migrated",
        "count": len(migrated),
        "target": str(target_file),
        "schedules": migrated,
    }

    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file.write_text(
            json.dumps({"schedules": migrated}, indent=2), encoding="utf-8"
        )

    click_echo(json.dumps(plan, indent=2))


# Suppress unused import warning — sys imported for potential future use
_ = sys
