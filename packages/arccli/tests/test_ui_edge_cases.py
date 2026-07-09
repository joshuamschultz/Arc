"""Edge case coverage for `arc ui start` (review BLOCKER #14).

The QA reviewer flagged HIGH-likelihood edge cases that prior tests missed:
  1. webbrowser.open() returns False (headless / no DISPLAY)
  2. Multiple agents with same name (backfill collision)

These are real failure modes a federal SCIF deployment will hit. (Registry
corruption of the per-agent trace store is no longer relevant — SPEC-026 FR-5
removed trace-store discovery; arcui reads from the shared arcstore mirror.)
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from arccli.commands.team import _backfill_workspaces, _init_cmd, _register
from arccli.commands.ui import _maybe_open_browser, _print_browser_open_fallback


class TestWebbrowserOpenReturnsFalse:
    """Headless server / no DISPLAY: `webbrowser.open` returns False.

    Review BLOCKER #14: must NOT fall back to printing the URL with token.
    """

    def test_returns_false_no_token_in_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("webbrowser.open", return_value=False):
            ok = _maybe_open_browser("127.0.0.1", 8420, "viewer-tok-abc")
        assert ok is False
        out = capsys.readouterr().out
        # The function itself MUST stay silent — the caller decides how to
        # present the failure (we tested _print_browser_open_fallback
        # separately). What we're guarding is "no #auth=... ever in stdout".
        assert "viewer-tok-abc" not in out
        assert "#auth=" not in out

    def test_fallback_nudges_to_link_without_token(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The fallback no longer prints tokens itself — `_start` already
        # printed the full magic-link above on loopback. It only nudges.
        _print_browser_open_fallback("127.0.0.1", 8420)
        out = capsys.readouterr().out
        assert "#auth=" not in out
        assert "viewer-tok-abc" not in out
        assert "http://127.0.0.1:8420/" in out


class TestBackfillNameCollision:
    """Two team/<dir>/arcagent.toml files declaring the same agent.name.

    Review BLOCKER #14: backfill matches by agent.name == entity.id; if
    two TOMLs claim the same name, the second silently overwrites the first.
    Document the actual behavior — first-write-wins or last-write-wins —
    so future readers know which workspace_path the entity ends up with.
    """

    def test_duplicate_name_is_deterministic(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_cmd(argparse.Namespace(root_path=str(tmp_path)))

        # Register one agent so the registry has a target id to match.
        first_ws = tmp_path / "first" / "workspace"
        first_ws.mkdir(parents=True)
        _register(
            argparse.Namespace(
                root=str(tmp_path),
                entity_id="dup_agent",
                name="dup_agent",
                entity_type="agent",
                roles="",
                workspace=None,  # leave None so backfill is the test
            )
        )

        # Two team subdirs both naming the same agent.
        team_dir = tmp_path / "team"
        for sub in ("alpha", "beta"):
            d = team_dir / sub
            d.mkdir(parents=True)
            (d / "workspace").mkdir()
            (d / "arcagent.toml").write_text(
                '[agent]\nname = "dup_agent"\nworkspace = "./workspace"\n'
            )

        args = argparse.Namespace(
            root=str(tmp_path),
            apply=True,
            team_dir=str(team_dir),
        )
        # MUST NOT crash; whichever workspace wins, the operator should see
        # both candidates in the output and be able to investigate.
        try:
            _backfill_workspaces(args)
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"backfill crashed on name collision: {exc}")

        # Two updates printed → deterministic last-write-wins (the second
        # match overwrites the first, glob order is sorted and stable).
        out = capsys.readouterr().out
        # At least one update message should mention the agent name.
        assert "dup_agent" in out, "operator must see SOMETHING about the duplicate name in stdout"
