"""Tests for UIAuditLogger — structured audit logging + OTel spans."""

from __future__ import annotations

import json
import logging

from arcui.audit import (
    AgentAutoconnectFields,
    SessionStartFields,
    UIAuditEvent,
    UIAuditLogger,
    _redact_sensitive,
)


class TestRedactSensitive:
    def test_redacts_token_keys(self):
        data = {"user": "alice", "token": "secret-123", "api_key": "key-456"}
        result = _redact_sensitive(data)
        assert result["user"] == "alice"
        assert result["token"] == "[REDACTED]"
        assert result["api_key"] == "[REDACTED]"

    def test_redacts_nested(self):
        data = {"outer": {"password": "hunter2", "name": "test"}}
        result = _redact_sensitive(data)
        assert result["outer"]["password"] == "[REDACTED]"
        assert result["outer"]["name"] == "test"

    def test_preserves_non_sensitive(self):
        data = {"agent_id": "a1", "action": "cancel", "status": "ok"}
        result = _redact_sensitive(data)
        assert result == data

    def test_empty_dict(self):
        assert _redact_sensitive({}) == {}


class TestRedactValuePatterns:
    """Wave 1 finding M-3: redact tokens that leak through innocuous keys."""

    def test_auth_hash_value_redacted_in_url(self) -> None:
        token = "a" * 64
        data = {"error": f"failed to fetch http://127.0.0.1:8420/#auth={token}"}
        result = _redact_sensitive(data)
        assert token not in result["error"]
        assert "[REDACTED]" in result["error"]

    def test_bearer_value_redacted(self) -> None:
        data = {"message": "rejected header: Bearer abc123def456"}
        result = _redact_sensitive(data)
        assert "abc123def456" not in result["message"]
        assert "[REDACTED]" in result["message"]

    def test_short_auth_pattern_not_falsely_redacted(self) -> None:
        # Below the 16-hex threshold — shouldn't be flagged as a token.
        data = {"detail": "auth=short"}
        result = _redact_sensitive(data)
        assert result["detail"] == "auth=short"

    def test_innocuous_string_unchanged(self) -> None:
        data = {"description": "user logged in successfully"}
        result = _redact_sensitive(data)
        assert result == data


class TestAuditEventTaxonomy:
    """Every emitter MUST reference UIAuditEvent (not raw strings)."""

    def test_session_start_value(self) -> None:
        assert UIAuditEvent.SESSION_START.value == "ui.session_start"

    def test_agent_autoconnect_value(self) -> None:
        assert UIAuditEvent.AGENT_AUTOCONNECT.value == "ui.agent_autoconnect"

    def test_enum_accepted_by_audit_event(self, caplog) -> None:
        logger = UIAuditLogger(enabled=False)
        with caplog.at_level(logging.INFO, logger="arcui.audit"):
            logger.audit_event(UIAuditEvent.SESSION_START, {"k": "v"})
        data = json.loads(caplog.records[0].message)
        assert data["event_type"] == "ui.session_start"


class TestTypedFieldSchemas:
    """Pydantic models enforce required-field shapes at construction.

    Drop-a-field becomes a ValidationError at the call site, not a
    silent audit gap an auditor finds 30 days later.
    """

    def test_session_start_fields_require_all_four(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SessionStartFields(  # type: ignore[call-arg]
                session_id="x",
                uid=1000,
                remote_addr="127.0.0.1",
                # auth_method missing
            )

    def test_agent_autoconnect_fields_require_all_four(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentAutoconnectFields(  # type: ignore[call-arg]
                agent_id="a1",
                uid=1000,
                url="ws://localhost",
                # reason missing
            )


class TestUIAuditLogger:
    def test_audit_event_logs_structured_json(self, caplog):
        logger = UIAuditLogger(enabled=False)
        with caplog.at_level(logging.INFO, logger="arcui.audit"):
            logger.audit_event("auth.success", {"agent_id": "a1", "role": "viewer"})

        assert len(caplog.records) == 1
        record = caplog.records[0]
        data = json.loads(record.message)
        assert data["event_type"] == "auth.success"
        assert data["details"]["agent_id"] == "a1"

    def test_audit_event_redacts_sensitive(self, caplog):
        logger = UIAuditLogger(enabled=False)
        with caplog.at_level(logging.INFO, logger="arcui.audit"):
            logger.audit_event("auth.attempt", {"token": "secret-abc"})

        data = json.loads(caplog.records[0].message)
        assert data["details"]["token"] == "[REDACTED]"

    def test_disabled_otel_still_logs(self, caplog):
        """Even with OTel disabled, structured logs are emitted."""
        logger = UIAuditLogger(enabled=False)
        with caplog.at_level(logging.INFO, logger="arcui.audit"):
            logger.audit_event("test.event", {"key": "value"})

        assert len(caplog.records) == 1
