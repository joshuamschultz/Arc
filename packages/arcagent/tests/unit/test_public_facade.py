"""Tests for ArcAgent's root integration facade."""

from __future__ import annotations

from pathlib import Path

import arcrun

import arcagent


def test_root_exports_resolve() -> None:
    for name in arcagent.__all__:
        assert getattr(arcagent, name) is not None


def test_stream_token_text_hides_arcrun_event_type() -> None:
    event = arcrun.TokenEvent(text="hello")

    assert arcagent.stream_token_text(event) == "hello"
    assert arcagent.stream_token_text(object()) is None


def test_model_config_path_routes_through_arcrun(monkeypatch) -> None:
    expected = Path("/model/config.toml")
    monkeypatch.setattr(arcrun, "model_config_path", lambda: expected)

    assert arcagent.model_config_path() == expected


def test_public_package_paths_exist() -> None:
    assert arcagent.builtin_capabilities_path().is_dir()
    assert arcagent.modules_path().is_dir()
