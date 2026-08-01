"""Shared fixtures for the ArcFlow definition-layer tests (SPEC-061)."""

from __future__ import annotations

from typing import Any

import pytest

# The DESIGN.md §4 example graph, already carrying the join="any" fix for the
# router-exclusivity deadlock the design's own example shipped with.
EXAMPLE_DOCUMENT: dict[str, Any] = {
    "workflow": {
        "schema_version": "1.0",
        "id": "customer-onboarding",
        "version": 4,
        "description": "New customer intake through provisioning",
        "owner": "@sales",
        "channel": "channel://onboarding",
        "budget": {"tokens": 400_000, "wall_clock_s": 1800},
    },
    "trigger": {
        "type": "cron",
        "expression": "0 9 * * MON",
        "active_hours": {
            "start": "08:00",
            "end": "18:00",
            "timezone": "America/Chicago",
        },
    },
    "input": {"schema": "schemas/onboarding_input.json"},
    "node": [
        {
            "id": "collect",
            "kind": "agent",
            "agent": "@sales",
            "skill": "customer-intake",
            "strategy": ["react"],
            "prompt": "prompts/collect.md",
            "output_schema": "schemas/customer_record.json",
            "artifacts": ["customer_record.json"],
            "timeout_s": 300,
            "max_attempts": 3,
        },
        {
            "id": "verify",
            "kind": "tool",
            "tool": "crm_lookup",
            "agent": "@sales",
            "needs": ["collect"],
            "args": {"domain": "$nodes.collect.output.company_domain"},
            "output_schema": "schemas/verification.json",
        },
        {
            "id": "risk_router",
            "kind": "router",
            "mode": "rules",
            "needs": ["verify"],
            "routes": [
                {"to": "provision", "when": "$nodes.verify.output.risk == 'low'"},
                {"to": "manual_review", "default": True},
            ],
        },
        {
            "id": "manual_review",
            "kind": "gate",
            "gate": "human:approve_high_risk",
            "needs": ["risk_router"],
        },
        {
            "id": "provision",
            "kind": "script",
            "script": "scripts/provision.py",
            "agent": "@ops",
            "needs": ["risk_router"],
            "output_schema": "schemas/provisioned.json",
        },
        {
            "id": "qa",
            "kind": "agent",
            "agent": "@reviewer",
            "needs": ["provision", "manual_review"],
            "join": "any",
            "output_schema": "schemas/qa_verdict.json",
        },
        {
            "id": "revise",
            "kind": "agent",
            "agent": "@ops",
            "needs": ["qa"],
            "when": "$nodes.qa.output.verdict == 'revise'",
            "loop_back_to": "provision",
            "max_iterations": 3,
        },
    ],
}


@pytest.fixture
def example_document() -> dict[str, Any]:
    """A deep copy of the canonical example so a test may mutate it freely."""
    import copy

    return copy.deepcopy(EXAMPLE_DOCUMENT)


def minimal_document(**workflow_overrides: Any) -> dict[str, Any]:
    """A two-node linear workflow — the smallest thing that validates."""
    workflow: dict[str, Any] = {"id": "tiny", "owner": "@sales"}
    workflow.update(workflow_overrides)
    return {
        "workflow": workflow,
        "node": [
            {"id": "a", "kind": "agent", "agent": "@sales"},
            {"id": "b", "kind": "agent", "agent": "@sales", "needs": ["a"]},
        ],
    }
