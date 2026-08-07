"""Activation approval for capability-spanning definitions (SPEC-061 COMP-016).

REQ-241: if a definition's nodes *jointly* span a forbidden capability
composition, an operator-signed grant is required at first activation and on
any edit that widens the composition. A narrowing edit must not re-prompt.

Why the union is computed here and not in arcteam: the tag-to-trifecta-leg
mapping is *deployment knowledge* and lives in
``core.session_internal.capability_ledger`` (the same reason arctrust receives
resolved frozensets rather than tags). arcteam owns the graph; arcagent owns
what a tool's tags mean.

**Scope of the static check, stated honestly.** Only statically-derivable legs
are counted: a ``tool`` node contributes its registered tool's tags, a
``script`` node contributes the untrusted-input leg of a subprocess. An
``agent`` node is a free agent — what it *actually* touches is not knowable
from the document, and pretending otherwise would mark every workflow as
spanning everything and train operators to click through. That runtime
composition is caught by the leg threader (COMP-015) feeding the real policy
pipeline on every node's tool calls, which is fail-closed. COMP-016 is the
pre-flight check on what a definition *declares*; COMP-015 is the enforcement.

Approved unions persist to the agent's WORKSPACE with direct filesystem I/O —
never through the model-facing file tools (ADR-029) — so a restart does not
re-prompt for a composition the operator already blessed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from arcagent.core.session_internal.capability_ledger import (
    LETHAL_TRIFECTA,
    UNTRUSTED_INPUT,
    legs_for_tags,
)

_logger = logging.getLogger("arcagent.modules.workflows.activation")

# Where blessed leg-unions are recorded, relative to the workflows bundle root.
_APPROVALS_FILE = ".activation-approvals.json"

# A script node runs code through the sandbox/execute machinery: it ingests
# untrusted content (command output, fetched files) exactly as a shell does.
_SCRIPT_LEGS = frozenset({UNTRUSTED_INPUT})


def union_of_legs(
    nodes: list[dict[str, Any]], tool_tags: dict[str, tuple[str, ...]]
) -> frozenset[str]:
    """The union of trifecta legs the definition's nodes can statically touch.

    ``tool_tags`` maps a registered tool name to its declared capability tags —
    supplied by the capability registry, which is the only source of truth for
    what a tool is allowed to do.
    """
    union: set[str] = set()
    for node in nodes:
        kind = node.get("kind")
        if kind == "tool":
            union |= legs_for_tags(tool_tags.get(str(node.get("tool", "")), ()))
        elif kind == "script":
            union |= _SCRIPT_LEGS
    return frozenset(union)


def spans_forbidden_composition(union: frozenset[str]) -> bool:
    """Whether the union completes the lethal trifecta (the forbidden set)."""
    return LETHAL_TRIFECTA <= union


def load_approvals(root: Path) -> list[frozenset[str]]:
    """Read the leg-unions an operator has already blessed for this agent."""
    path = root / _APPROVALS_FILE
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A corrupt approvals file must never silently grant: treat it as empty
        # so the next activation re-prompts (fail-closed).
        _logger.warning("Unreadable activation approvals at %s; treating as empty", path)
        return []
    return [frozenset(entry) for entry in raw if isinstance(entry, list)]


def record_approval(root: Path, union: frozenset[str]) -> None:
    """Persist a blessed leg-union to the agent's workspace (direct I/O)."""
    approvals = load_approvals(root)
    if any(union <= approved for approved in approvals):
        return
    approvals.append(union)
    root.mkdir(parents=True, exist_ok=True)
    (root / _APPROVALS_FILE).write_text(
        json.dumps([sorted(entry) for entry in approvals], indent=2), encoding="utf-8"
    )


def already_approved(root: Path, union: frozenset[str]) -> bool:
    """True if ``union`` is contained in a previously blessed union.

    Containment — not equality — is what makes a *narrowing* edit free: a
    definition that drops a node can only shrink its union, and a shrunken
    union is already covered by the grant the operator gave for the wider one.
    A widening edit produces a union no prior grant contains, so it re-prompts.
    """
    return any(union <= approved for approved in load_approvals(root))


async def require_activation_grant(
    *,
    human_gate: Any,
    root: Path,
    workflow_id: str,
    content_hash: str,
    agent_did: str,
    union: frozenset[str],
) -> str | None:
    """Return None when activation may proceed, or a refusal reason.

    Reuses the existing human-gate / approval-grant machinery: the grant is
    signed by the OPERATOR key, bound to this definition's content hash through
    the call arguments, and the agent has no path to mint it (ASI09).
    """
    if not spans_forbidden_composition(union):
        return None
    if already_approved(root, union):
        return None
    if human_gate is None:
        return (
            f"workflow '{workflow_id}' spans the forbidden capability composition "
            f"{sorted(union)} and no operator approval channel is reachable"
        )

    from arcllm import configured_redactor
    from arctrust.policy import ToolCall

    # Built here, never through arcllm, so the deployment's PII policy has not
    # run on it. Applied at construction so the gate can present what it was
    # handed without deciding a policy it does not own.
    redact = configured_redactor()
    call = ToolCall(
        tool_name="workflow_activate",
        arguments={"workflow_id": redact(workflow_id), "content_hash": content_hash},
        agent_did=agent_did,
        session_id=f"workflow:{workflow_id}",
        classification="unclassified",
        capability_tags=frozenset(union),
    )
    grant = await human_gate.request(call, legs=union)
    if grant is None:
        return (
            f"activation of workflow '{workflow_id}' requires an operator-signed "
            f"approval for the composition {sorted(union)}; none was granted"
        )
    record_approval(root, union)
    return None


__all__ = [
    "already_approved",
    "load_approvals",
    "record_approval",
    "require_activation_grant",
    "spans_forbidden_composition",
    "union_of_legs",
]
