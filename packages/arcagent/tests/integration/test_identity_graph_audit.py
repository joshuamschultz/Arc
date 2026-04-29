"""Integration test: federal audit-event emission for IdentityGraph.

Verifies that every state-changing operation on IdentityGraph emits the
correct gateway.identity.{link,unlink} audit event through AgentTelemetry.
This is the federal compliance requirement (NIST 800-53 AU-2/AU-9).

Key assertions:
  - gateway.identity.link emitted on insert-on-first-seen
  - gateway.identity.link emitted on explicit link_identities
  - gateway.identity.unlink emitted on unlink_identity
  - Audit event details contain user_did, platform, linked_by_did, ts
  - Audit event details contain platform_user_id_hash (NOT raw platform_user_id)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.core.config import TelemetryConfig
from arcagent.core.telemetry import AgentTelemetry
from arcagent.modules.session.identity_graph import IdentityGraph

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_telemetry_config(enabled: bool = False) -> TelemetryConfig:
    """Return a minimal TelemetryConfig (OTel disabled; audit log enabled)."""
    return TelemetryConfig(enabled=enabled)


def _sha256_prefix(value: str, chars: int = 16) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:chars]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sessions" / "identity_graph.db"


@pytest.fixture
def captured_events() -> list[dict[str, Any]]:
    """Shared list that stores every (event_type, details) call."""
    return []


@pytest.fixture
def spy_telemetry(captured_events: list[dict[str, Any]]) -> MagicMock:
    """Mock telemetry that records audit_event calls into captured_events."""
    m = MagicMock(spec=AgentTelemetry)

    def _record(event_type: str, details: dict[str, Any]) -> None:
        captured_events.append({"event_type": event_type, "details": details})

    m.audit_event.side_effect = _record
    return m


# ---------------------------------------------------------------------------
# Tests: insert-on-first-seen audit event
# ---------------------------------------------------------------------------


def test_first_seen_emits_link_event(
    db_path: Path,
    spy_telemetry: MagicMock,
    captured_events: list[dict[str, Any]],
) -> None:
    """resolve_user_identity for a new identity must emit gateway.identity.link."""
    graph = IdentityGraph(db_path=db_path, telemetry=spy_telemetry)
    did = graph.resolve_user_identity("telegram", "user_001")

    link_events = [e for e in captured_events if e["event_type"] == "gateway.identity.link"]
    assert len(link_events) >= 1, "Expected at least one gateway.identity.link event"

    evt = link_events[0]
    assert evt["details"]["user_did"] == did
    assert evt["details"]["platform"] == "telegram"


def test_first_seen_audit_event_has_hashed_user_id(
    db_path: Path,
    spy_telemetry: MagicMock,
    captured_events: list[dict[str, Any]],
) -> None:
    """Audit event for insert-on-first-seen must contain platform_user_id_hash,
    NOT raw platform_user_id (PII protection — LLM02 mitigation)."""
    raw_uid = "pii_sensitive_user_999"
    expected_hash = _sha256_prefix(raw_uid)

    graph = IdentityGraph(db_path=db_path, telemetry=spy_telemetry)
    graph.resolve_user_identity("telegram", raw_uid)

    for evt in captured_events:
        details_str = json.dumps(evt["details"])
        assert raw_uid not in details_str, f"Raw platform_user_id leaked into audit event: {evt}"

    link_events = [e for e in captured_events if e["event_type"] == "gateway.identity.link"]
    assert any(
        evt["details"].get("platform_user_id_hash") == expected_hash for evt in link_events
    ), f"Expected platform_user_id_hash={expected_hash} in one of {link_events}"


# ---------------------------------------------------------------------------
# Tests: explicit link_identities audit event
# ---------------------------------------------------------------------------


def test_explicit_link_emits_link_event(
    db_path: Path,
    spy_telemetry: MagicMock,
    captured_events: list[dict[str, Any]],
) -> None:
    """link_identities must emit gateway.identity.link for the newly-linked pair."""
    graph = IdentityGraph(db_path=db_path, telemetry=spy_telemetry)
    did = graph.resolve_user_identity("telegram", "user_001")
    captured_events.clear()  # Reset — only care about the explicit link call

    graph.link_identities(
        user_did=did,
        platform="slack",
        platform_user_id="U789",
        linked_by_did="did:arc:ops:admin/abcdef01",
    )

    link_events = [e for e in captured_events if e["event_type"] == "gateway.identity.link"]
    assert len(link_events) >= 1

    evt = link_events[-1]
    assert evt["details"]["user_did"] == did
    assert evt["details"]["platform"] == "slack"
    assert evt["details"]["linked_by_did"] == "did:arc:ops:admin/abcdef01"
    assert "ts" in evt["details"]


def test_explicit_link_audit_event_has_hashed_user_id(
    db_path: Path,
    spy_telemetry: MagicMock,
    captured_events: list[dict[str, Any]],
) -> None:
    """Explicit link audit event must not contain raw platform_user_id."""
    raw_uid = "slack_pii_user"
    expected_hash = _sha256_prefix(raw_uid)

    graph = IdentityGraph(db_path=db_path, telemetry=spy_telemetry)
    did = graph.resolve_user_identity("telegram", "tg_001")

    graph.link_identities(
        user_did=did,
        platform="slack",
        platform_user_id=raw_uid,
        linked_by_did="did:arc:ops:admin/abcdef01",
    )

    for evt in captured_events:
        if evt["event_type"] == "gateway.identity.link":
            details_str = json.dumps(evt["details"])
            assert raw_uid not in details_str

    link_events = [
        e
        for e in captured_events
        if e["event_type"] == "gateway.identity.link" and e["details"].get("platform") == "slack"
    ]
    assert any(evt["details"].get("platform_user_id_hash") == expected_hash for evt in link_events)


# ---------------------------------------------------------------------------
# Tests: unlink audit event
# ---------------------------------------------------------------------------


def test_unlink_emits_unlink_event(
    db_path: Path,
    spy_telemetry: MagicMock,
    captured_events: list[dict[str, Any]],
) -> None:
    """unlink_identity must emit gateway.identity.unlink."""
    graph = IdentityGraph(db_path=db_path, telemetry=spy_telemetry)
    did = graph.resolve_user_identity("telegram", "user_001")
    captured_events.clear()

    graph.unlink_identity(did, "telegram", "user_001")

    unlink_events = [e for e in captured_events if e["event_type"] == "gateway.identity.unlink"]
    assert len(unlink_events) >= 1

    evt = unlink_events[0]
    assert evt["details"]["user_did"] == did
    assert evt["details"]["platform"] == "telegram"
    assert "ts" in evt["details"]


def test_unlink_audit_event_hashes_user_id(
    db_path: Path,
    spy_telemetry: MagicMock,
    captured_events: list[dict[str, Any]],
) -> None:
    """Unlink audit event must not contain raw platform_user_id."""
    raw_uid = "unlink_pii_user"

    graph = IdentityGraph(db_path=db_path, telemetry=spy_telemetry)
    did = graph.resolve_user_identity("telegram", raw_uid)
    captured_events.clear()

    graph.unlink_identity(did, "telegram", raw_uid)

    for evt in captured_events:
        details_str = json.dumps(evt["details"])
        assert raw_uid not in details_str, (
            f"Raw platform_user_id leaked in unlink audit event: {evt}"
        )


# ---------------------------------------------------------------------------
# Tests: no-telemetry mode (graceful degradation)
# ---------------------------------------------------------------------------


def test_graph_works_without_telemetry(db_path: Path) -> None:
    """IdentityGraph operates correctly when no telemetry is injected."""
    graph = IdentityGraph(db_path=db_path, telemetry=None)
    did = graph.resolve_user_identity("telegram", "user_no_telemetry")
    assert did.startswith("did:arc:")
    graph.link_identities(
        user_did=did,
        platform="slack",
        platform_user_id="U_no_tel",
        linked_by_did="did:arc:ops:admin/00000000",
    )
    graph.unlink_identity(did, "slack", "U_no_tel")
    links = graph.list_links(did)
    assert len(links) == 1  # only telegram remains
