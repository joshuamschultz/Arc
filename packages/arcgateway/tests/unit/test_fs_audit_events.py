"""Verifies fs_reader and fs_watcher emit NIST AU-2 audit events with required fields.

NIST 800-53 AU-2 requires the following recorded for security-relevant events:
- WHO  : actor_did (caller_did in our extra)
- WHAT : action (gateway.fs.read | gateway.fs.tree | gateway.fs.changed)
- WHERE: target (path)
- WHEN : timestamp (auto-populated by AuditEvent)
- HOW  : outcome (allow | deny | error)
- agent_id, scope (custom Arc fields in extra)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.audit import AuditEvent

from arcgateway import audit as gw_audit
from arcgateway.fs_reader import list_tree, read_file


class _CapturingSink:
    """Test sink — records every event written, verifies AuditSink Protocol."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> _CapturingSink:
    """Replace the gateway audit sink with a capturing one for the test."""
    sink = _CapturingSink()
    gw_audit.configure_sink(sink, actor_did="did:arc:gateway:test")
    yield sink
    # Reset to NullSink so tests don't bleed.
    from arctrust.audit import NullSink

    gw_audit.configure_sink(NullSink(), actor_did="did:arc:gateway:daemon")


@pytest.fixture
def agent_root(tmp_path: Path) -> Path:
    root = tmp_path / "team" / "alice_agent"
    (root / "workspace").mkdir(parents=True)
    (root / "arcagent.toml").write_text("[agent]\nname='alice'\n", encoding="utf-8")
    (root / "workspace" / "policy.md").write_text("# policy\n", encoding="utf-8")
    return root


class TestReadEmitsAudit:
    def test_read_file_emits_gateway_fs_read(
        self, captured: _CapturingSink, agent_root: Path
    ) -> None:
        read_file(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            rel_path="arcagent.toml",
            caller_did="did:arc:org:operator/josh",
        )
        actions = [e.action for e in captured.events]
        assert "gateway.fs.read" in actions

    def test_audit_event_includes_required_au2_fields(
        self, captured: _CapturingSink, agent_root: Path
    ) -> None:
        read_file(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            rel_path="arcagent.toml",
            caller_did="did:arc:org:operator/josh",
        )
        evt = next(e for e in captured.events if e.action == "gateway.fs.read")
        # WHO — actor_did is the gateway's; the human caller DID is in extra.
        assert evt.actor_did
        assert evt.extra.get("caller_did") == "did:arc:org:operator/josh"
        # WHAT — action.
        assert evt.action == "gateway.fs.read"
        # WHERE — path on target.
        assert "arcagent.toml" in evt.target
        # WHEN — ts auto-populated.
        assert evt.ts is not None
        # HOW — outcome.
        assert evt.outcome == "allow"
        # Custom Arc fields.
        assert evt.extra.get("agent_id") == "alice"
        assert evt.extra.get("scope") == "agent"


class TestTreeEmitsAudit:
    def test_list_tree_emits_gateway_fs_tree(
        self, captured: _CapturingSink, agent_root: Path
    ) -> None:
        list_tree(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            caller_did="did:arc:org:operator/josh",
        )
        actions = [e.action for e in captured.events]
        assert "gateway.fs.tree" in actions

    def test_tree_event_records_caller_and_agent(
        self, captured: _CapturingSink, agent_root: Path
    ) -> None:
        list_tree(
            scope="agent",
            agent_id="alice",
            agent_root=agent_root,
            rel_path="workspace",
            caller_did="did:arc:org:operator/josh",
        )
        evt = next(e for e in captured.events if e.action == "gateway.fs.tree")
        assert evt.extra.get("agent_id") == "alice"
        assert evt.extra.get("path") == "workspace"
        assert evt.extra.get("caller_did") == "did:arc:org:operator/josh"
