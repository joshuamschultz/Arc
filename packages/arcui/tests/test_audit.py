"""Tests for UIAuditLogger — structured audit logging + OTel spans."""

from __future__ import annotations

import json
import logging

from arcui.audit import UIAuditLogger, _redact_sensitive


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
