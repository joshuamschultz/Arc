"""Tests for `arc ui start` zero-arg behavior (SPEC-019 T3.1, T3.2, T3.3).

These exercise the helpers (_resolve_trace_stores, _maybe_open_browser)
without actually starting uvicorn — that path is exercised by an integration
test elsewhere.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from arccli.commands.team import _init_cmd, _register
from arccli.commands.ui import _maybe_open_browser, _resolve_trace_stores


def _bootstrap_team(tmp_path: Path) -> Path:
    """Init a team root and register one agent with a workspace."""
    _init_cmd(argparse.Namespace(root_path=str(tmp_path)))
    workspace = tmp_path / "ws_a1"
    workspace.mkdir()
    _register(
        argparse.Namespace(
            root=str(tmp_path),
            entity_id="agent://a1",
            name="A1",
            entity_type="agent",
            roles="",
            workspace=str(workspace),
        )
    )
    return tmp_path


# ---------------------------------------------------------------------------
# _resolve_trace_stores — registry-driven only (FR-4)
# ---------------------------------------------------------------------------


class TestResolveTraceStoresFromRegistry:
    """Registry is queried unconditionally; no flag overrides."""

    def test_skips_entities_without_workspace(self, tmp_path: Path) -> None:
        _bootstrap_team(tmp_path)
        # Register a second agent WITHOUT a workspace path (user type skips it)
        _register(
            argparse.Namespace(
                root=str(tmp_path),
                entity_id="user://u1",
                name="U1",
                entity_type="user",
                roles="",
                workspace=None,
            )
        )
        args = argparse.Namespace(root=str(tmp_path))
        stores = _resolve_trace_stores(args)
        # Only the agent with workspace_path becomes a store
        assert len(stores) == 1

    def test_skips_entities_with_missing_workspace_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_cmd(argparse.Namespace(root_path=str(tmp_path)))
        # Register an agent then delete its workspace dir
        ws = tmp_path / "deleted_workspace"
        ws.mkdir()
        _register(
            argparse.Namespace(
                root=str(tmp_path),
                entity_id="agent://gone",
                name="Gone",
                entity_type="agent",
                roles="",
                workspace=str(ws),
            )
        )
        ws.rmdir()

        args = argparse.Namespace(root=str(tmp_path))
        stores = _resolve_trace_stores(args)
        assert stores == []
        out = capsys.readouterr().out
        assert "skip" in out.lower() or "not found" in out.lower()


# ---------------------------------------------------------------------------
# _maybe_open_browser
# ---------------------------------------------------------------------------


class TestMaybeOpenBrowserLoopback:
    """Loopback bind opens browser with token in URL hash."""

    def test_loopback_opens_browser(self) -> None:
        with patch("webbrowser.open", return_value=True) as mock_open:
            _maybe_open_browser("127.0.0.1", 8420, "viewer_token_value")
        mock_open.assert_called_once()
        url = mock_open.call_args[0][0]
        assert "#auth=viewer_token_value" in url
        assert "127.0.0.1:8420" in url

    def test_localhost_opens_browser(self) -> None:
        with patch("webbrowser.open", return_value=True) as mock_open:
            _maybe_open_browser("localhost", 8420, "tok")
        mock_open.assert_called_once()


class TestMaybeOpenBrowserNonLoopback:
    """Non-loopback bind MUST NOT open the browser (SR-4)."""

    def test_zero_zero_zero_zero_skips(self) -> None:
        with patch("webbrowser.open") as mock_open:
            _maybe_open_browser("0.0.0.0", 8420, "tok")  # noqa: S104 — testing non-loopback path
        mock_open.assert_not_called()

    def test_external_ip_skips(self) -> None:
        with patch("webbrowser.open") as mock_open:
            _maybe_open_browser("10.0.0.1", 8420, "tok")
        mock_open.assert_not_called()
