"""AuditRedactor — full connector capture with one credential carve-out (COMP-014).

Covers REQ-276 (every connector call and response recorded in full — inputs AND
outputs — with classification labels, so an auditor can reconstruct what happened
rather than merely that it happened, NIST AU-3) and REQ-277 (a credential read
records store, item, field, caller, and outcome, never the value).

Three failure modes get their own tests because each one turns the tamper-evident
audit chain into the richest credential database on the box:

* **A carve-out keyed only on pattern matching.** Regex detection has false
  negatives by construction, so ``test_credential_value_the_detector_cannot_see_
  is_still_never_recorded`` uses a credential no ``SECRET_PATTERNS`` entry can
  match. A redactor that scans instead of reading the tool's declaration passes
  every other test here and leaks in production.
* **Redaction in the wrong package.** ``test_arctrust_does_not_import_arcllm``
  pins the dependency direction: ``arctrust`` is a leaf, so the scrub has to run
  here, before the event reaches ``arctrust.audit.emit``. Inverting the DAG kills
  standalone packaging.
* **A bespoke sink.** ``test_emission_goes_through_the_single_chokepoint`` proves
  CON-5 — events reach the sink through ``arctrust.audit.emit``, never a direct
  ``sink.write``, or the tamper-evident chain and the compliance sinks diverge.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Any

import arctrust.audit
import pytest
from arctrust.audit import AuditEvent

from arcagent.core.tier import Tier
from arcagent.extension.attachment import ToolOutcome, ToolResult, ToolSpec

if TYPE_CHECKING:
    from arcagent.extension.audit import AuditRedactor

AGENT_DID = "did:arc:test:agent/abc123"

# A real AWS access key shape — SECRET_PATTERNS matches this one.
DETECTABLE_SECRET = "AKIAIOSFODNN7EXAMPLE"

# A credential no pattern in arcllm/_secrets.py matches. This is the whole point
# of keying the carve-out off the declaration.
UNDETECTABLE_SECRET = "hunter2-correct-horse"


class CollectingSink:
    """An AuditSink that keeps every event for inspection."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def sink() -> CollectingSink:
    return CollectingSink()


@pytest.fixture
def redactor(sink: CollectingSink) -> AuditRedactor:
    from arcagent.extension.audit import AuditRedactor

    return AuditRedactor(audit_sink=sink, tier=Tier.FEDERAL)


def ordinary_tool() -> ToolSpec:
    return ToolSpec(
        name="create_issue",
        classification="state_modifying",
        capability_tags=["network_egress"],
    )


def credential_tool() -> ToolSpec:
    from arcagent.extension.audit import CREDENTIAL_READ_TAG

    return ToolSpec(
        name="read_token",
        classification="read_only",
        capability_tags=[CREDENTIAL_READ_TAG],
    )


def blob(event: AuditEvent) -> str:
    """Every recorded byte of one event, as one searchable string."""
    return event.model_dump_json()


# ---------------------------------------------------------------------------
# REQ-276 — full capture
# ---------------------------------------------------------------------------


def test_ordinary_call_records_its_inputs_in_full(
    redactor: AuditRedactor, sink: CollectingSink
) -> None:
    """An auditor must be able to see what was asked, not just that something was."""
    redactor.record_call(
        caller_did=AGENT_DID,
        instance="github_work",
        tool=ordinary_tool(),
        args={"repo": "arc", "title": "Broken loader", "labels": ["bug", "p0"]},
        result=ToolResult(tool="create_issue", content="created #41"),
    )

    assert len(sink.events) == 1
    recorded = blob(sink.events[0])
    assert "Broken loader" in recorded
    assert "arc" in recorded
    assert "p0" in recorded


def test_ordinary_call_records_its_response_in_full(
    redactor: AuditRedactor, sink: CollectingSink
) -> None:
    """Outputs are half of reconstruction (AU-3) — inputs alone are not enough."""
    redactor.record_call(
        caller_did=AGENT_DID,
        instance="github_work",
        tool=ordinary_tool(),
        args={"repo": "arc"},
        result=ToolResult(tool="create_issue", content="created #41 at 2026-08-06T00:00:00Z"),
    )

    assert "created #41 at 2026-08-06T00:00:00Z" in blob(sink.events[0])


def test_record_carries_the_tool_classification_label(
    redactor: AuditRedactor, sink: CollectingSink
) -> None:
    """REQ-276 asks for classification labels; the tool declares its own."""
    redactor.record_call(
        caller_did=AGENT_DID,
        instance="github_work",
        tool=ordinary_tool(),
        args={},
        result=ToolResult(tool="create_issue"),
    )

    assert sink.events[0].extra["classification"] == "state_modifying"


def test_record_names_caller_target_and_outcome(
    redactor: AuditRedactor, sink: CollectingSink
) -> None:
    """Who did what to what, and how it ended."""
    redactor.record_call(
        caller_did=AGENT_DID,
        instance="github_work",
        tool=ordinary_tool(),
        args={},
        result=ToolResult(tool="create_issue"),
    )

    event = sink.events[0]
    assert event.actor_did == AGENT_DID
    assert event.action == "connector.call"
    assert "github_work" in event.target
    assert "create_issue" in event.target
    assert event.outcome == ToolOutcome.OK.value
    assert event.tier == Tier.FEDERAL.value


def test_failed_call_records_the_failure_text(
    redactor: AuditRedactor, sink: CollectingSink
) -> None:
    """A failure is exactly the call an auditor most wants to reconstruct."""
    redactor.record_call(
        caller_did=AGENT_DID,
        instance="github_work",
        tool=ordinary_tool(),
        args={"repo": "arc"},
        result=ToolResult(
            tool="create_issue",
            outcome=ToolOutcome.ERROR,
            content="upstream returned 403 forbidden",
        ),
    )

    event = sink.events[0]
    assert event.outcome == ToolOutcome.ERROR.value
    assert "upstream returned 403 forbidden" in blob(event)


# ---------------------------------------------------------------------------
# REQ-277 — the one carve-out, on both paths
# ---------------------------------------------------------------------------


def test_credential_read_records_the_coordinates(
    redactor: AuditRedactor, sink: CollectingSink
) -> None:
    """Store, item, field, caller, outcome — everything but the value."""
    redactor.record_credential_read(
        caller_did=AGENT_DID,
        store="local",
        item="github_work",
        field="api_token",
        outcome="allow",
    )

    event = sink.events[0]
    assert event.actor_did == AGENT_DID
    assert event.action == "credential.read"
    assert event.outcome == "allow"
    assert event.extra["store"] == "local"
    assert event.extra["item"] == "github_work"
    assert event.extra["field"] == "api_token"


def test_credential_read_through_a_declared_tool_never_records_the_value(
    redactor: AuditRedactor, sink: CollectingSink
) -> None:
    """The declaration path: the tool says it reads a credential, so the value drops."""
    redactor.record_call(
        caller_did=AGENT_DID,
        instance="github_work",
        tool=credential_tool(),
        args={"store": "local", "item": "github_work", "field": "api_token"},
        result=ToolResult(tool="read_token", content=DETECTABLE_SECRET),
    )

    event = sink.events[0]
    assert DETECTABLE_SECRET not in blob(event)
    assert event.extra["store"] == "local"
    assert event.extra["item"] == "github_work"
    assert event.extra["field"] == "api_token"


def test_credential_value_the_detector_cannot_see_is_still_never_recorded(
    redactor: AuditRedactor, sink: CollectingSink
) -> None:
    """Regex has false negatives. A carve-out that only scans is not a carve-out.

    ``UNDETECTABLE_SECRET`` matches no pattern in ``arcllm/_secrets.py``; only a
    redactor keyed off the tool's DECLARATION keeps it out of the chain.
    """
    redactor.record_call(
        caller_did=AGENT_DID,
        instance="github_work",
        tool=credential_tool(),
        args={"store": "local", "item": "github_work", "field": "api_token"},
        result=ToolResult(tool="read_token", content=UNDETECTABLE_SECRET),
    )

    assert UNDETECTABLE_SECRET not in blob(sink.events[0])


def test_secret_leaking_through_an_ordinary_call_is_redacted_by_pattern(
    redactor: AuditRedactor, sink: CollectingSink
) -> None:
    """The pattern path: an undeclared tool whose payload happens to carry a secret.

    Full capture is the rule, so this is defence in depth rather than the carve-out
    — but a detectable credential must never reach the chain verbatim.
    """
    redactor.record_call(
        caller_did=AGENT_DID,
        instance="github_work",
        tool=ordinary_tool(),
        args={"body": f"deploy key {DETECTABLE_SECRET}"},
        result=ToolResult(tool="create_issue", content=f"echoed {DETECTABLE_SECRET}"),
    )

    recorded = blob(sink.events[0])
    assert DETECTABLE_SECRET not in recorded
    assert "AWS_ACCESS_KEY" in recorded
    # Full capture still holds around the redaction.
    assert "deploy key" in recorded
    assert "echoed" in recorded


# ---------------------------------------------------------------------------
# Architectural invariants
# ---------------------------------------------------------------------------


def test_emission_goes_through_the_single_chokepoint(
    monkeypatch: pytest.MonkeyPatch, sink: CollectingSink
) -> None:
    """CON-5: events reach a sink via ``arctrust.audit.emit``, never a bespoke write."""
    from arcagent.extension import audit as audit_module

    seen: list[tuple[AuditEvent, Any]] = []

    def spy(event: AuditEvent, target: Any) -> None:
        seen.append((event, target))

    monkeypatch.setattr(audit_module, "emit", spy)

    audit_module.AuditRedactor(audit_sink=sink, tier=Tier.PERSONAL).record_call(
        caller_did=AGENT_DID,
        instance="github_work",
        tool=ordinary_tool(),
        args={},
        result=ToolResult(tool="create_issue"),
    )

    assert len(seen) == 1
    assert seen[0][1] is sink
    assert sink.events == []


def test_redaction_completes_before_the_event_reaches_emit(
    monkeypatch: pytest.MonkeyPatch, sink: CollectingSink
) -> None:
    """The scrub runs HERE. What ``emit`` receives is already clean."""
    from arcagent.extension import audit as audit_module

    seen: list[AuditEvent] = []
    monkeypatch.setattr(audit_module, "emit", lambda event, _sink: seen.append(event))

    audit_module.AuditRedactor(audit_sink=sink, tier=Tier.PERSONAL).record_call(
        caller_did=AGENT_DID,
        instance="github_work",
        tool=ordinary_tool(),
        args={"body": DETECTABLE_SECRET},
        result=ToolResult(tool="create_issue"),
    )

    assert DETECTABLE_SECRET not in blob(seen[0])


def test_arctrust_does_not_import_arcllm() -> None:
    """``arctrust`` is a leaf. Redaction lives here precisely because of this.

    If the leaf ever imports ``arcllm`` the dependency DAG inverts and standalone
    packaging of the trust foundation dies — so the scrub can never move down.
    """
    root = Path(arctrust.audit.__file__).parent
    offenders: list[str] = []

    for source in root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] == "arcllm" for name in names):
                offenders.append(f"{source.name}:{node.lineno}")

    assert offenders == [], f"arctrust must not import arcllm: {offenders}"
