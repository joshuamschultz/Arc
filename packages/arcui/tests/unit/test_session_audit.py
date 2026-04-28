"""Tests for UI session_start audit emission (SPEC-019 T5.3, SR-3).

Validates that the auth middleware emits exactly one `ui.session_start` per
unique viewer/operator token+remote_addr combination, with the correct
auth_method label.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker


def _build_app(auth: AuthConfig, audit: UIAuditLogger, tracker: SessionTracker) -> Starlette:
    async def _ping(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api/ping", _ping)])
    app.state.audit = audit
    app.state.session_tracker = tracker
    app.add_middleware(AuthMiddleware, auth_config=auth)
    return app


def _capture_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for rec in caplog.records:
        try:
            data = json.loads(rec.message)
        except json.JSONDecodeError:
            continue
        events.append(data)
    return events


class TestSessionStartEmittedOncePerSession:
    """First authenticated request emits, second from same session does not."""

    def test_first_request_emits_session_start(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        auth = AuthConfig()
        audit = UIAuditLogger(enabled=False)
        tracker = SessionTracker()
        app = _build_app(auth, audit, tracker)

        client = TestClient(app)
        with caplog.at_level(logging.INFO, logger="arcui.audit"):
            client.get(
                "/api/ping",
                headers={"Authorization": f"Bearer {auth.viewer_token}"},
            )

        events = _capture_events(caplog)
        starts = [e for e in events if e["event_type"] == "ui.session_start"]
        assert len(starts) == 1

    def test_second_request_does_not_re_emit(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        auth = AuthConfig()
        audit = UIAuditLogger(enabled=False)
        tracker = SessionTracker()
        app = _build_app(auth, audit, tracker)
        client = TestClient(app)

        with caplog.at_level(logging.INFO, logger="arcui.audit"):
            for _ in range(3):
                client.get(
                    "/api/ping",
                    headers={"Authorization": f"Bearer {auth.viewer_token}"},
                )

        events = _capture_events(caplog)
        starts = [e for e in events if e["event_type"] == "ui.session_start"]
        assert len(starts) == 1


class TestSessionStartAuthMethodLabel:
    """The auth_method label distinguishes browser_bootstrap from manual_token.

    A session whose token was issued via URL hash on loopback receives
    `browser_bootstrap`. Anything else (manual paste, off-loopback) is
    `manual_token`. The tracker is informed of bootstrap-issued tokens by
    `_maybe_open_browser` at startup.
    """

    def test_unmarked_token_is_manual(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        auth = AuthConfig()
        audit = UIAuditLogger(enabled=False)
        tracker = SessionTracker()
        app = _build_app(auth, audit, tracker)
        client = TestClient(app)

        with caplog.at_level(logging.INFO, logger="arcui.audit"):
            client.get(
                "/api/ping",
                headers={"Authorization": f"Bearer {auth.viewer_token}"},
            )

        events = _capture_events(caplog)
        starts = [e for e in events if e["event_type"] == "ui.session_start"]
        assert len(starts) == 1
        details = starts[0]["details"]
        assert details["auth_method"] == "manual_token"

    def test_bootstrap_marked_token_is_browser_bootstrap(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        auth = AuthConfig()
        audit = UIAuditLogger(enabled=False)
        tracker = SessionTracker()
        # Mark the viewer token as having been delivered via URL hash —
        # mirrors the call _maybe_open_browser makes on loopback start.
        tracker.mark_bootstrap_issued(auth.viewer_token)

        app = _build_app(auth, audit, tracker)
        client = TestClient(app)

        with caplog.at_level(logging.INFO, logger="arcui.audit"):
            client.get(
                "/api/ping",
                headers={"Authorization": f"Bearer {auth.viewer_token}"},
            )

        events = _capture_events(caplog)
        starts = [e for e in events if e["event_type"] == "ui.session_start"]
        assert len(starts) == 1
        assert starts[0]["details"]["auth_method"] == "browser_bootstrap"


class TestSessionStartHasRequiredFields:
    """SR-3: every ui.session_start MUST carry session_id, uid, remote_addr,
    auth_method. Field-level assertions catch emissions that report some
    fields but quietly drop others — federal auditors won't accept that.
    """

    def test_all_four_fields_present_with_correct_types(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import os

        auth = AuthConfig()
        audit = UIAuditLogger(enabled=False)
        tracker = SessionTracker()
        app = _build_app(auth, audit, tracker)
        client = TestClient(app)

        with caplog.at_level(logging.INFO, logger="arcui.audit"):
            client.get(
                "/api/ping",
                headers={"Authorization": f"Bearer {auth.viewer_token}"},
            )

        events = _capture_events(caplog)
        starts = [e for e in events if e["event_type"] == "ui.session_start"]
        assert len(starts) == 1
        details = starts[0]["details"]

        # session_id: 16-hex-char string (secrets.token_hex(8) → 16 chars).
        assert "session_id" in details
        assert isinstance(details["session_id"], str)
        assert len(details["session_id"]) == 16

        # uid: int matching the server-process UID at request time.
        assert "uid" in details
        assert isinstance(details["uid"], int)
        assert details["uid"] == os.getuid()

        # remote_addr: TestClient reports "testclient" by default.
        assert "remote_addr" in details
        assert isinstance(details["remote_addr"], str)

        # auth_method: one of three labels per SR-3.
        assert "auth_method" in details
        assert details["auth_method"] in {
            "browser_bootstrap", "manual_token", "agent_token"
        }

    def test_session_id_unique_per_session(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two different (token, addr) pairs MUST get different session_ids.

        Two callers sharing one session_id would let an auditor mistakenly
        attribute one user's actions to another.
        """
        auth = AuthConfig()
        audit = UIAuditLogger(enabled=False)
        tracker = SessionTracker()
        # First request: viewer
        # Second request: operator (different token = different session)
        app = _build_app(auth, audit, tracker)
        client = TestClient(app)

        with caplog.at_level(logging.INFO, logger="arcui.audit"):
            client.get(
                "/api/ping",
                headers={"Authorization": f"Bearer {auth.viewer_token}"},
            )
            client.get(
                "/api/ping",
                headers={"Authorization": f"Bearer {auth.operator_token}"},
            )

        events = _capture_events(caplog)
        starts = [e for e in events if e["event_type"] == "ui.session_start"]
        assert len(starts) == 2
        ids = {s["details"]["session_id"] for s in starts}
        assert len(ids) == 2, "two distinct sessions must yield two session_ids"
