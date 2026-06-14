"""SPEC-017 Phase 8 — policy / completion / schedule introspection helpers.

Thin Python functions wrapping core APIs. Each returns a JSON-serializable
dict; callers that want CLI exposure can register them through the slash-
command registry. (Original Click groups were dead-coded — never wired into
the arccli command tree — so the CLI shell was removed to satisfy the
"no click in arccli" architecture rule. The behavior is preserved.)

Public exports: ``policy_layers``, ``policy_evaluate``,
``completion_history``, ``schedule_list``, ``schedule_migrate``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

Tier = Literal["federal", "enterprise", "personal"]


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def policy_layers(tier: Tier = "personal") -> dict[str, Any]:
    """Return the layers active for the given tier."""
    from arcagent.core.tool_policy import build_pipeline

    pipeline = build_pipeline(tier=tier)
    return {
        "tier": tier,
        "layers": [layer.name for layer in pipeline.layers],
    }


def policy_evaluate(
    *,
    tool_name: str,
    tier: Tier = "personal",
    agent_did: str = "did:arc:cli",
    classification: str = "unclassified",
) -> dict[str, Any]:
    """Dry-run a tool-call decision and return the serialized verdict.

    Signs the probe call with an ephemeral identity (and admits it) so the
    fail-closed IdentityLayer passes — the dry-run reports the *authorization*
    verdict (global/agent/sandbox) for the tool, not an authentication failure.
    The ``agent_did`` argument is retained for the audit-style payload only.
    """
    from arcagent.core.tool_policy import PolicyContext, ToolCall, build_pipeline, sign_call
    from arctrust import AgentIdentity

    probe = AgentIdentity.generate(org="cli", agent_type="probe")
    pipeline = build_pipeline(tier=tier, agent_registry={probe.did: probe.public_key})
    call = sign_call(
        ToolCall(
            tool_name=tool_name,
            arguments={},
            agent_did=agent_did,
            session_id="cli",
            classification=classification,
        ),
        probe,
    )
    ctx = PolicyContext(tier=tier, policy_version="v1", bundle_age_seconds=0.0)
    decision = asyncio.run(pipeline.evaluate(call, ctx))
    return decision.model_dump()


# ---------------------------------------------------------------------------
# completion
# ---------------------------------------------------------------------------


def completion_history(path: str = ".", limit: int = 20) -> dict[str, Any]:
    """Return the most recent ``loop.completed`` events from the audit log.

    Reads ``workspace/audit/*.jsonl`` and filters for completion events.
    """
    agent_dir = Path(path).resolve()
    audit_dir = agent_dir / "workspace" / "audit"
    if not audit_dir.exists():
        return {"events": []}

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
    return {"events": events}


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------


def schedule_list(path: str = ".") -> dict[str, Any]:
    """Return persisted schedules from ``workspace/proactive/schedules.json``."""
    agent_dir = Path(path).resolve()
    state_file = agent_dir / "workspace" / "proactive" / "schedules.json"
    if not state_file.exists():
        return {"schedules": []}
    data: dict[str, Any] = json.loads(state_file.read_text(encoding="utf-8"))
    return data


def schedule_migrate(path: str = ".", *, dry_run: bool = False) -> dict[str, Any]:
    """One-time migration from legacy ``workspace/scheduler/`` JSONL state.

    SPEC-017 R-040 deleted ``modules/scheduler/`` and its persisted state
    format. Deployments upgrading from arc-agent < 0.3.0 must run this once
    to convert their old schedule definitions into the proactive engine's
    ``workspace/proactive/schedules.json`` layout.
    """
    agent_dir = Path(path).resolve()
    legacy_dir = agent_dir / "workspace" / "scheduler"
    target_dir = agent_dir / "workspace" / "proactive"
    target_file = target_dir / "schedules.json"

    if not legacy_dir.exists():
        return {
            "status": "no-op",
            "reason": "legacy scheduler/ directory not present",
            "legacy_dir": str(legacy_dir),
        }

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

    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file.write_text(
            json.dumps({"schedules": migrated}, indent=2),
            encoding="utf-8",
        )

    return {
        "status": "dry-run" if dry_run else "migrated",
        "count": len(migrated),
        "target": str(target_file),
        "schedules": migrated,
    }
