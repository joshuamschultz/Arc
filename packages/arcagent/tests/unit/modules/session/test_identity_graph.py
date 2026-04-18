"""Unit tests for arcagent.modules.session.identity_graph (IdentityGraph).

TDD red-phase tests written before the implementation.  Covers:
  - Insert-on-first-seen returns a stable user_did
  - Second call to resolve_user_identity returns the SAME user_did
  - Cross-platform linking: link telegram:123 to an existing user_did;
    subsequent resolve of linked platform returns that user_did
  - Audit event emitted on link (mock telemetry)
  - Unlink removes the row but does NOT delete the user_did
  - Concurrent inserts on same (platform, user_id) — exactly one row
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.modules.session.identity_graph import IdentityGraph, Link


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Isolated SQLite path for each test."""
    return tmp_path / "sessions" / "identity_graph.db"


@pytest.fixture
def graph(db_path: Path) -> IdentityGraph:
    """IdentityGraph with a fresh in-test database."""
    return IdentityGraph(db_path=db_path)


@pytest.fixture
def mock_telemetry() -> MagicMock:
    """Mock AgentTelemetry that records audit_event calls."""
    m = MagicMock()
    m.audit_event = MagicMock()
    return m


# ---------------------------------------------------------------------------
# T1: Insert-on-first-seen stability
# ---------------------------------------------------------------------------


def test_resolve_new_identity_returns_did(graph: IdentityGraph) -> None:
    """First call for an unknown (platform, user_id) generates a user_did."""
    did = graph.resolve_user_identity("telegram", "123")
    assert did.startswith("did:arc:"), f"Expected DID format, got: {did}"


def test_resolve_same_identity_twice_returns_same_did(graph: IdentityGraph) -> None:
    """Repeated calls for the same (platform, user_id) return the same user_did."""
    did_first = graph.resolve_user_identity("telegram", "123")
    did_second = graph.resolve_user_identity("telegram", "123")
    assert did_first == did_second


def test_different_platforms_get_different_dids_by_default(graph: IdentityGraph) -> None:
    """Without explicit linking, two platforms produce distinct user_dids."""
    did_telegram = graph.resolve_user_identity("telegram", "123")
    did_slack = graph.resolve_user_identity("slack", "U456")
    assert did_telegram != did_slack


# ---------------------------------------------------------------------------
# T2: Lookup (read-only)
# ---------------------------------------------------------------------------


def test_lookup_unknown_returns_none(graph: IdentityGraph) -> None:
    """lookup_user_did returns None for an unknown (platform, user_id)."""
    result = graph.lookup_user_did("telegram", "999")
    assert result is None


def test_lookup_known_returns_did(graph: IdentityGraph) -> None:
    """lookup_user_did returns the stored user_did after resolve."""
    did = graph.resolve_user_identity("telegram", "42")
    found = graph.lookup_user_did("telegram", "42")
    assert found == did


# ---------------------------------------------------------------------------
# T3: Explicit link_identities + cross-platform resolution
# ---------------------------------------------------------------------------


def test_link_identities_cross_platform(graph: IdentityGraph) -> None:
    """Linking telegram:123 to an existing user_did means slack:U456 resolves
    to that same user_did after link_identities is called."""
    original_did = graph.resolve_user_identity("telegram", "123")

    # Link slack:U456 to the same user
    graph.link_identities(
        user_did=original_did,
        platform="slack",
        platform_user_id="U456",
        linked_by_did="did:arc:ops:admin/deadbeef",
    )

    # Resolve the slack identity — must return the SAME user_did
    slack_did = graph.resolve_user_identity("slack", "U456")
    assert slack_did == original_did


def test_link_identities_does_not_overwrite_existing(graph: IdentityGraph) -> None:
    """link_identities on an already-linked pair is a no-op (no error, no data loss)."""
    did = graph.resolve_user_identity("telegram", "123")
    # Link same pair twice — should not raise
    graph.link_identities(
        user_did=did,
        platform="telegram",
        platform_user_id="123",
        linked_by_did="did:arc:ops:admin/deadbeef",
    )
    # Still resolves to the same DID
    assert graph.resolve_user_identity("telegram", "123") == did


# ---------------------------------------------------------------------------
# T4: list_links
# ---------------------------------------------------------------------------


def test_list_links_returns_all_platforms(graph: IdentityGraph) -> None:
    """list_links returns all linked platforms for a user_did."""
    did = graph.resolve_user_identity("telegram", "123")
    graph.link_identities(
        user_did=did,
        platform="slack",
        platform_user_id="U456",
        linked_by_did="did:arc:ops:admin/deadbeef",
    )

    links = graph.list_links(did)
    platforms = {lnk.platform for lnk in links}
    assert "telegram" in platforms
    assert "slack" in platforms


def test_list_links_empty_for_unknown_did(graph: IdentityGraph) -> None:
    """list_links returns [] for a user_did that has no rows."""
    links = graph.list_links("did:arc:user:human/nonexistent")
    assert links == []


def test_link_has_expected_fields(graph: IdentityGraph) -> None:
    """Each Link has user_did, platform, platform_user_id, linked_at, linked_by_did."""
    did = graph.resolve_user_identity("telegram", "123")
    links = graph.list_links(did)
    assert len(links) == 1
    lnk = links[0]
    assert isinstance(lnk, Link)
    assert lnk.user_did == did
    assert lnk.platform == "telegram"
    assert lnk.platform_user_id == "123"
    assert lnk.linked_at > 0
    assert lnk.linked_by_did != ""


# ---------------------------------------------------------------------------
# T5: Unlink
# ---------------------------------------------------------------------------


def test_unlink_removes_row(graph: IdentityGraph) -> None:
    """unlink_identity removes the (platform, platform_user_id) row."""
    did = graph.resolve_user_identity("telegram", "123")
    graph.unlink_identity(did, "telegram", "123")
    assert graph.lookup_user_did("telegram", "123") is None


def test_unlink_does_not_remove_other_links(graph: IdentityGraph) -> None:
    """Unlinking one platform does not affect other linked platforms."""
    did = graph.resolve_user_identity("telegram", "123")
    graph.link_identities(
        user_did=did,
        platform="slack",
        platform_user_id="U456",
        linked_by_did="did:arc:ops:admin/deadbeef",
    )
    graph.unlink_identity(did, "telegram", "123")

    # slack link must survive
    assert graph.lookup_user_did("slack", "U456") == did


def test_unlink_nonexistent_is_noop(graph: IdentityGraph) -> None:
    """Unlinking a row that does not exist does not raise."""
    graph.unlink_identity("did:arc:user:human/ghost", "telegram", "999")


# ---------------------------------------------------------------------------
# T6: Audit event emission
# ---------------------------------------------------------------------------


def test_link_emits_audit_event(db_path: Path, mock_telemetry: MagicMock) -> None:
    """link_identities emits a gateway.identity.link audit event."""
    graph = IdentityGraph(db_path=db_path, telemetry=mock_telemetry)
    did = graph.resolve_user_identity("telegram", "123")

    graph.link_identities(
        user_did=did,
        platform="slack",
        platform_user_id="U456",
        linked_by_did="did:arc:ops:admin/deadbeef",
    )

    mock_telemetry.audit_event.assert_called()
    call_args = mock_telemetry.audit_event.call_args_list
    event_types = [c.args[0] for c in call_args]
    assert "gateway.identity.link" in event_types


def test_resolve_first_seen_emits_link_audit_event(
    db_path: Path, mock_telemetry: MagicMock
) -> None:
    """resolve_user_identity on a new (platform, user_id) also emits an audit event."""
    graph = IdentityGraph(db_path=db_path, telemetry=mock_telemetry)
    graph.resolve_user_identity("telegram", "123")

    mock_telemetry.audit_event.assert_called()


def test_unlink_emits_audit_event(db_path: Path, mock_telemetry: MagicMock) -> None:
    """unlink_identity emits a gateway.identity.unlink audit event."""
    graph = IdentityGraph(db_path=db_path, telemetry=mock_telemetry)
    did = graph.resolve_user_identity("telegram", "123")
    mock_telemetry.audit_event.reset_mock()

    graph.unlink_identity(did, "telegram", "123")

    mock_telemetry.audit_event.assert_called()
    call_args = mock_telemetry.audit_event.call_args_list
    event_types = [c.args[0] for c in call_args]
    assert "gateway.identity.unlink" in event_types


def test_audit_event_hashes_platform_user_id(
    db_path: Path, mock_telemetry: MagicMock
) -> None:
    """Audit events must NOT log raw platform_user_id — only its SHA-256 prefix."""
    graph = IdentityGraph(db_path=db_path, telemetry=mock_telemetry)
    graph.resolve_user_identity("telegram", "sensitive_user_id_12345")

    # Inspect all audit calls: raw user_id must not appear in details
    for call in mock_telemetry.audit_event.call_args_list:
        details: dict[str, Any] = call.args[1] if len(call.args) > 1 else {}
        details_str = str(details)
        assert "sensitive_user_id_12345" not in details_str, (
            "Raw platform_user_id leaked into audit event details"
        )


# ---------------------------------------------------------------------------
# T7: Concurrent-insert safety (PRIMARY KEY constraint)
# ---------------------------------------------------------------------------


async def test_concurrent_inserts_produce_single_row(tmp_path: Path) -> None:
    """Twenty concurrent resolve_user_identity calls for the same
    (platform, user_id) must produce exactly one row (no duplicate user_dids).

    This validates that the SQLite PRIMARY KEY constraint on (platform,
    platform_user_id) prevents race-induced duplicate rows when multiple
    coroutines attempt insert-on-first-seen simultaneously.
    """
    db_path = tmp_path / "sessions" / "identity_graph.db"
    # All 20 coroutines share the same IdentityGraph instance so they share
    # the same connection (synchronous SQLite serialises via GIL anyway),
    # but we also test isolation across instances below.
    graph = IdentityGraph(db_path=db_path)

    results = await asyncio.gather(
        *[
            asyncio.to_thread(graph.resolve_user_identity, "telegram", "concurrent_user")
            for _ in range(20)
        ]
    )

    # All resolved DIDs must be identical
    unique_dids = set(results)
    assert len(unique_dids) == 1, (
        f"Expected exactly 1 unique user_did, got {len(unique_dids)}: {unique_dids}"
    )

    # Confirm exactly one row in the DB
    links = graph.list_links(next(iter(unique_dids)))
    assert len(links) == 1


async def test_concurrent_inserts_across_instances(tmp_path: Path) -> None:
    """Multiple IdentityGraph instances opening the same DB file must still
    produce exactly one row for the same (platform, user_id).

    This is the realistic multi-worker scenario where the DB file is shared
    via filesystem.
    """
    db_path = tmp_path / "sessions" / "identity_graph.db"

    async def resolve_with_fresh_instance() -> str:
        g = IdentityGraph(db_path=db_path)
        return await asyncio.to_thread(g.resolve_user_identity, "slack", "shared_user")

    results = await asyncio.gather(*[resolve_with_fresh_instance() for _ in range(20)])

    unique_dids = set(results)
    assert len(unique_dids) == 1, (
        f"Expected exactly 1 unique user_did from multi-instance run, "
        f"got {len(unique_dids)}: {unique_dids}"
    )
