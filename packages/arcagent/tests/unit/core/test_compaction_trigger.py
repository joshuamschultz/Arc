"""Tests for session-owns-context wiring and compaction trigger in chat()."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from arcagent.core.config import ContextConfig, SessionConfig
from arcagent.core.session_manager import SessionManager


class TestSessionOwnsContext:
    """T5.1: Session-owns-context wiring."""

    def test_accepts_context_manager_param(self, tmp_path: Path) -> None:
        """SessionManager accepts context_manager parameter."""
        ctx_mgr = MagicMock()
        session = SessionManager(
            config=SessionConfig(),
            context_config=ContextConfig(),
            telemetry=MagicMock(),
            workspace=tmp_path,
            context_manager=ctx_mgr,
        )
        assert session.context_manager is ctx_mgr

    def test_token_ratio_delegates_to_context_manager(self, tmp_path: Path) -> None:
        """session.token_ratio() delegates to context_manager."""
        ctx_mgr = MagicMock()
        ctx_mgr.token_ratio.return_value = 0.75
        session = SessionManager(
            config=SessionConfig(),
            context_config=ContextConfig(),
            telemetry=MagicMock(),
            workspace=tmp_path,
            context_manager=ctx_mgr,
        )
        assert session.token_ratio() == 0.75
        ctx_mgr.token_ratio.assert_called_once()

    def test_token_ratio_returns_zero_without_context_manager(self, tmp_path: Path) -> None:
        """token_ratio returns 0.0 when no context_manager set."""
        session = SessionManager(
            config=SessionConfig(),
            context_config=ContextConfig(),
            telemetry=MagicMock(),
            workspace=tmp_path,
        )
        assert session.token_ratio() == 0.0

    def test_context_manager_property(self, tmp_path: Path) -> None:
        """context_manager property returns the stored context manager."""
        ctx_mgr = MagicMock()
        session = SessionManager(
            config=SessionConfig(),
            context_config=ContextConfig(),
            telemetry=MagicMock(),
            workspace=tmp_path,
            context_manager=ctx_mgr,
        )
        assert session.context_manager is ctx_mgr
