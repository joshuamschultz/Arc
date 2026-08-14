"""Unit tests for arctui entry orchestration (flags + endpoint resolution)."""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.paths import arc_team

from arctui.entry import _flag_value, _maybe_prompt_trust, _resolve_endpoint, _resolve_team_root
from arctui.serve import Endpoint, GatewayNeedsTokenError
from arctui.trust import folder_is_trusted


def _agent_toml(tmp_path: Path) -> Path:
    cfg = tmp_path / "coder" / "arcagent.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '[agent]\nname = "coder"\n[tools.policy]\nallowed_paths = []\n', encoding="utf-8"
    )
    return cfg


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
    # The default is whatever the ONE resolver says; re-spelling it here is
    # how this assertion outlived the layout it described.
    assert _resolve_team_root([]) == arc_team()


def test_resolve_team_root_explicit() -> None:
    assert _resolve_team_root(["--team-root", "/data/work"]) == Path("/data/work")


def test_resolve_team_root_root_alias() -> None:
    # `arc tui --root <path>` — explicit path override.
    assert _resolve_team_root(["--root", ".arc/coding"]) == Path(".arc/coding")


def test_resolve_team_root_team_is_global() -> None:
    # `arc tui --team coding` resolves the GLOBAL fleet (findable from any cwd).
    assert _resolve_team_root(["--team", "coding"]) == arc_team("coding")


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


# --------------------------------------------------------------------------- #
# _maybe_prompt_trust — the folder-trust grant path (security-relevant)
# --------------------------------------------------------------------------- #


def test_trust_prompt_grants_on_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _agent_toml(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    _maybe_prompt_trust(cfg, proj)
    assert folder_is_trusted(cfg, proj) is True


def test_trust_prompt_declines_on_no(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _agent_toml(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    _maybe_prompt_trust(cfg, proj)
    assert folder_is_trusted(cfg, proj) is False


def test_trust_prompt_non_interactive_does_not_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _agent_toml(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def _boom(_prompt: str) -> str:  # input() must never be called non-interactively
        raise AssertionError("prompted on non-interactive stdin")

    monkeypatch.setattr("builtins.input", _boom)
    _maybe_prompt_trust(cfg, proj)
    assert folder_is_trusted(cfg, proj) is False


def test_trust_prompt_already_trusted_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _agent_toml(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    from arctui.trust import grant_folder

    grant_folder(cfg, proj)  # pre-trust it
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _boom(_prompt: str) -> str:  # no prompt when already trusted
        raise AssertionError("prompted for an already-trusted folder")

    monkeypatch.setattr("builtins.input", _boom)
    _maybe_prompt_trust(cfg, proj)  # must not raise
