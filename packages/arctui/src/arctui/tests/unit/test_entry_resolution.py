"""Unit tests for arctui entry orchestration (flags + endpoint resolution)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arctui.entry import _flag_value, _resolve_endpoint, _resolve_team_root
from arctui.serve import Endpoint, GatewayNeedsTokenError

# --------------------------------------------------------------------------- #
# _flag_value
# --------------------------------------------------------------------------- #


def test_flag_value_space_form() -> None:
    assert _flag_value(["--agent", "employee"], "--agent") == "employee"


def test_flag_value_equals_form() -> None:
    assert _flag_value(["--agent=coder"], "--agent") == "coder"


def test_flag_value_missing_returns_none() -> None:
    assert _flag_value(["--other", "x"], "--agent") is None


# --------------------------------------------------------------------------- #
# _resolve_team_root
# --------------------------------------------------------------------------- #


def test_resolve_team_root_default() -> None:
    assert _resolve_team_root([]) == Path.home() / ".arc" / "team"


def test_resolve_team_root_explicit() -> None:
    assert _resolve_team_root(["--team-root", "/data/work"]) == Path("/data/work")


# --------------------------------------------------------------------------- #
# _resolve_endpoint — explicit --url attaches, never spawns
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_url_with_token_attaches_without_spawn() -> None:
    ep = await _resolve_endpoint(
        ["--url", "http://box:8420/", "--token", "tok"], "employee", Path("/team")
    )
    assert ep == Endpoint("http://box:8420", "tok", "employee", spawned=False)


@pytest.mark.asyncio
async def test_url_without_token_raises_needs_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Point the persisted-token lookup at an empty dir so none is found.
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    with pytest.raises(GatewayNeedsTokenError):
        await _resolve_endpoint(["--url", "http://box:8420"], "employee", Path("/team"))
