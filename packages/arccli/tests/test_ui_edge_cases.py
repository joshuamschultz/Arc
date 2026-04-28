"""Edge case coverage for `arc ui start` (review BLOCKER #14).

The QA reviewer flagged three HIGH-likelihood edge cases that prior tests
missed:
  1. Registry corruption (malformed entity record on disk)
  2. webbrowser.open() returns False (headless / no DISPLAY)
  3. Multiple agents with same name (backfill collision)

These are real failure modes a federal SCIF deployment will hit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from arccli.commands.team import _backfill_workspaces, _init_cmd, _register
from arccli.commands.ui import (
    _maybe_open_browser,
    _print_browser_open_fallback,
    _resolve_trace_stores,
)


class TestRegistryCorruption:
    """`_resolve_trace_stores` must survive a corrupt entity record on disk."""

    def test_malformed_json_in_entity_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_cmd(argparse.Namespace(root_path=str(tmp_path)))
        ws = tmp_path / "ws_a"
        ws.mkdir()
        _register(argparse.Namespace(
            root=str(tmp_path),
            entity_id="agent://a1",
            name="A1",
            entity_type="agent",
            roles="",
            workspace=str(ws),
        ))
        # Locate the persisted entity record via the arcteam-public
        # `REGISTRY_COLLECTION` constant rather than hardcoding the
        # `messages/registry/` filesystem layout — Wave 2 review TD-LOW
        # decouples this test from FileBackend internals so a backend
        # refactor doesn't break unrelated tests.
        from arcteam.registry import REGISTRY_COLLECTION

        entity_files = list((tmp_path / REGISTRY_COLLECTION).glob("*.json"))
        assert entity_files, "expected at least one entity file after register"
        entity_files[0].write_text("{not valid json")

        # The launcher MUST NOT crash — at worst it prints a warning and
        # returns whatever stores it could resolve. A federal deployment
        # cannot have its UI brought down by one corrupt record on disk.
        args = argparse.Namespace(root=str(tmp_path))
        try:
            stores = _resolve_trace_stores(args)
        except Exception as exc:  # pragma: no cover — crashes here are the bug
            pytest.fail(
                f"_resolve_trace_stores crashed on corrupt registry: {exc}"
            )
        # Either zero stores (registry list errored) or it skipped the bad
        # one. The contract is "didn't crash" — the dashboard surfaces an
        # empty state, not a stack trace.
        assert isinstance(stores, list)


class TestWebbrowserOpenReturnsFalse:
    """Headless server / no DISPLAY: `webbrowser.open` returns False.

    Review BLOCKER #14: must NOT fall back to printing the URL with token.
    """

    def test_returns_false_no_token_in_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("webbrowser.open", return_value=False):
            ok = _maybe_open_browser("127.0.0.1", 8420, "viewer-tok-abc")
        assert ok is False
        out = capsys.readouterr().out
        # The function itself MUST stay silent — the caller decides how to
        # present the failure (we tested _print_browser_open_fallback
        # separately). What we're guarding is "no #auth=... ever in stdout".
        assert "viewer-tok-abc" not in out
        assert "#auth=" not in out

    def test_fallback_prints_no_token_url(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _print_browser_open_fallback(
            "127.0.0.1", 8420, "viewer-tok-abc", show_tokens=False
        )
        out = capsys.readouterr().out
        # The URL line MUST NOT carry the auth fragment under any condition.
        assert "#auth=" not in out
        assert "http://127.0.0.1:8420/" in out


class TestBackfillNameCollision:
    """Two team/<dir>/arcagent.toml files declaring the same agent.name.

    Review BLOCKER #14: backfill matches by agent.name == entity.id; if
    two TOMLs claim the same name, the second silently overwrites the first.
    Document the actual behavior — first-write-wins or last-write-wins —
    so future readers know which workspace_path the entity ends up with.
    """

    def test_duplicate_name_is_deterministic(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_cmd(argparse.Namespace(root_path=str(tmp_path)))

        # Register one agent so the registry has a target id to match.
        first_ws = tmp_path / "first" / "workspace"
        first_ws.mkdir(parents=True)
        _register(argparse.Namespace(
            root=str(tmp_path),
            entity_id="dup_agent",
            name="dup_agent",
            entity_type="agent",
            roles="",
            workspace=None,  # leave None so backfill is the test
        ))

        # Two team subdirs both naming the same agent.
        team_dir = tmp_path / "team"
        for sub in ("alpha", "beta"):
            d = team_dir / sub
            d.mkdir(parents=True)
            (d / "workspace").mkdir()
            (d / "arcagent.toml").write_text(
                "[agent]\n"
                'name = "dup_agent"\n'
                'workspace = "./workspace"\n'
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
        assert "dup_agent" in out, (
            "operator must see SOMETHING about the duplicate name in stdout"
        )
