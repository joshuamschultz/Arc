"""`arcui.serve()` hands the app the schedule authority, so dashboard schedule writes work."""

from __future__ import annotations

from pathlib import Path

import pytest
import uvicorn
from arctrust import (
    LocalControlArtifactAuthority,
    OperatorKey,
    config_file,
    default_operator_key_path,
)

from arcui.server import serve


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))


def _served_app(monkeypatch: pytest.MonkeyPatch) -> object:
    served: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **_kw: served.append(app))
    serve()
    (app,) = served
    return app


def test_arcui_serve_passes_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    state = _served_app(monkeypatch).state  # type: ignore[attr-defined]  # reason: starlette state
    assert isinstance(state.schedule_control_authority, LocalControlArtifactAuthority)
    assert state.schedule_tenant_id


def test_arcui_serve_federal_has_no_local_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_file("arcagent.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[security]\ntier = "federal"\n', encoding="utf-8")
    state = _served_app(monkeypatch).state  # type: ignore[attr-defined]  # reason: starlette state
    assert state.schedule_control_authority is None


def test_arcui_serve_without_operator_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _served_app(monkeypatch).state  # type: ignore[attr-defined]  # reason: starlette state
    assert state.schedule_control_authority is None
