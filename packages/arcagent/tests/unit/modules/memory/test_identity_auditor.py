"""Tests for IdentityAuditor — identity.md audit trail."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.core.module_bus import EventContext
from arcagent.modules.memory.markdown_memory import IdentityAuditor


def _make_telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


def _make_ctx(data: dict[str, Any] | None = None) -> EventContext:
    return EventContext(
        event="agent:pre_tool",
        data=data or {},
        agent_did="did:arc:test",
        trace_id="trace-1",
    )


class TestCaptureBefore:
    """T3.4.1: Snapshots current content."""

    @pytest.mark.asyncio()
    async def test_captures_existing_content(self, tmp_path: Path) -> None:
        telemetry = _make_telemetry()
        auditor = IdentityAuditor(tmp_path, telemetry)
        identity = tmp_path / "identity.md"
        identity.write_text("original content")

        ctx = _make_ctx()
        await auditor.capture_before(ctx, identity)
        assert auditor._before_snapshots["trace-1"] == "original content"

    @pytest.mark.asyncio()
    async def test_captures_empty_when_no_file(self, tmp_path: Path) -> None:
        telemetry = _make_telemetry()
        auditor = IdentityAuditor(tmp_path, telemetry)
        identity = tmp_path / "identity.md"
        # File doesn't exist

        ctx = _make_ctx()
        await auditor.capture_before(ctx, identity)
        assert auditor._before_snapshots["trace-1"] == ""


class TestCaptureAfter:
    """T3.4.2: Emits audit event with before/after."""

    @pytest.mark.asyncio()
    async def test_emits_audit_event_on_change(self, tmp_path: Path) -> None:
        telemetry = _make_telemetry()
        auditor = IdentityAuditor(tmp_path, telemetry)
        identity = tmp_path / "identity.md"
        identity.write_text("original")

        # Capture before
        ctx = _make_ctx({"session_id": "sess-123"})
        await auditor.capture_before(ctx, identity)

        # Simulate write
        identity.write_text("modified content")

        # Capture after
        await auditor.capture_after(ctx, identity)

        # Verify telemetry event
        telemetry.audit_event.assert_called_once()
        call_args = telemetry.audit_event.call_args
        assert call_args[0][0] == "identity.modified"
        assert call_args[0][1]["before_length"] == len("original")
        assert call_args[0][1]["after_length"] == len("modified content")

    @pytest.mark.asyncio()
    async def test_includes_session_id(self, tmp_path: Path) -> None:
        telemetry = _make_telemetry()
        auditor = IdentityAuditor(tmp_path, telemetry)
        identity = tmp_path / "identity.md"
        identity.write_text("v1")

        ctx = _make_ctx({"session_id": "my-session-id"})
        await auditor.capture_before(ctx, identity)
        identity.write_text("v2")
        await auditor.capture_after(ctx, identity)

        call_details = telemetry.audit_event.call_args[0][1]
        assert call_details["session_id"] == "my-session-id"


class TestJSONLAuditFile:
    """T3.4.3: Appends entry with all required fields."""

    @pytest.mark.asyncio()
    async def test_creates_audit_file(self, tmp_path: Path) -> None:
        telemetry = _make_telemetry()
        auditor = IdentityAuditor(tmp_path, telemetry)
        identity = tmp_path / "identity.md"
        identity.write_text("before")

        ctx = _make_ctx({"session_id": "s1"})
        await auditor.capture_before(ctx, identity)
        identity.write_text("after")
        await auditor.capture_after(ctx, identity)

        audit_file = tmp_path / "audit" / "identity-changes.jsonl"
        assert audit_file.exists()

    @pytest.mark.asyncio()
    async def test_audit_entry_has_required_fields(self, tmp_path: Path) -> None:
        telemetry = _make_telemetry()
        auditor = IdentityAuditor(tmp_path, telemetry)
        identity = tmp_path / "identity.md"
        identity.write_text("before")

        ctx = _make_ctx({"session_id": "s1"})
        await auditor.capture_before(ctx, identity)
        identity.write_text("after")
        await auditor.capture_after(ctx, identity)

        audit_file = tmp_path / "audit" / "identity-changes.jsonl"
        content = audit_file.read_text().strip()
        entry = json.loads(content)

        assert "timestamp" in entry
        assert "agent_did" in entry
        assert entry["before"] == "before"
        assert entry["after"] == "after"
        assert entry["session_id"] == "s1"

    @pytest.mark.asyncio()
    async def test_appends_multiple_entries(self, tmp_path: Path) -> None:
        telemetry = _make_telemetry()
        auditor = IdentityAuditor(tmp_path, telemetry)
        identity = tmp_path / "identity.md"

        # First change
        identity.write_text("v1")
        ctx = _make_ctx({"session_id": "s1"})
        await auditor.capture_before(ctx, identity)
        identity.write_text("v2")
        await auditor.capture_after(ctx, identity)

        # Second change
        ctx2 = _make_ctx({"session_id": "s2"})
        await auditor.capture_before(ctx2, identity)
        identity.write_text("v3")
        await auditor.capture_after(ctx2, identity)

        audit_file = tmp_path / "audit" / "identity-changes.jsonl"
        lines = audit_file.read_text().strip().split("\n")
        assert len(lines) == 2


class TestNoChangeDetection:
    """T3.4.4: Same content produces no audit event."""

    @pytest.mark.asyncio()
    async def test_no_change_skips_audit(self, tmp_path: Path) -> None:
        telemetry = _make_telemetry()
        auditor = IdentityAuditor(tmp_path, telemetry)
        identity = tmp_path / "identity.md"
        identity.write_text("unchanged content")

        ctx = _make_ctx()
        await auditor.capture_before(ctx, identity)
        # Content not modified
        await auditor.capture_after(ctx, identity)

        telemetry.audit_event.assert_not_called()
        audit_file = tmp_path / "audit" / "identity-changes.jsonl"
        assert not audit_file.exists()


class TestFirstWrite:
    """T3.4.5: No existing file results in empty 'before'."""

    @pytest.mark.asyncio()
    async def test_first_write_has_empty_before(self, tmp_path: Path) -> None:
        telemetry = _make_telemetry()
        auditor = IdentityAuditor(tmp_path, telemetry)
        identity = tmp_path / "identity.md"
        # File doesn't exist yet

        ctx = _make_ctx({"session_id": "first"})
        await auditor.capture_before(ctx, identity)
        identity.write_text("first write content")
        await auditor.capture_after(ctx, identity)

        audit_file = tmp_path / "audit" / "identity-changes.jsonl"
        entry = json.loads(audit_file.read_text().strip())
        assert entry["before"] == ""
        assert entry["after"] == "first write content"
