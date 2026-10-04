"""Settings -> Maintenance -> Updates: switch or roll back the installed runtime.

The dashboard moves ``runtime/current`` through the same
:func:`arctrust.paths.activate_runtime` ``arc runtime activate`` uses, then asks the
existing stack restart to bring the new version up. A viewer may look; only an
operator may switch; and a version is always a bare name of something installed,
never a path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from arctrust import paths
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.routes import stack as stack_routes
from arcui.server import create_app

OP_TOKEN = "o" * 64
VIEW_TOKEN = "v" * 64


class AuditRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def audit_event(self, name: str, fields: dict[str, Any]) -> None:
        self.events.append(dict(fields))

    def outcomes(self, operation: str) -> list[str]:
        return [e["outcome"] for e in self.events if e.get("operation") == operation]


@pytest.fixture
def arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "arc-home"
    monkeypatch.setenv("ARC_CONFIG_DIR", str(root))
    for index, version in enumerate(("0.2.0-aaaaaaa", "0.2.0-bbbbbbb")):
        directory = root / "runtime" / version
        directory.mkdir(parents=True)
        os.utime(directory, (1_700_000_000 + index * 86_400,) * 2)
    paths.activate_runtime("0.2.0-bbbbbbb")
    return root


@pytest.fixture
def restarts(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    launched: list[list[str]] = []
    monkeypatch.setattr(stack_routes, "_spawn_restart", launched.append)
    return launched


@pytest.fixture
def audit() -> AuditRecorder:
    return AuditRecorder()


@pytest.fixture
def client(arc_home: Path, audit: AuditRecorder, tmp_path: Path) -> TestClient:
    app = create_app(
        auth_config=AuthConfig({"viewer_token": VIEW_TOKEN, "operator_token": OP_TOKEN}),
        team_root=tmp_path / "team",
    )
    app.state.audit = audit
    return TestClient(app)


def _op() -> dict[str, str]:
    return {"Authorization": f"Bearer {OP_TOKEN}"}


def _viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEW_TOKEN}"}


def test_a_viewer_sees_installed_versions_and_which_is_live(client: TestClient) -> None:
    resp = client.get("/api/maintenance/runtime", headers=_viewer())

    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] == "0.2.0-bbbbbbb"
    by_name = {v["version"]: v for v in body["versions"]}
    assert by_name["0.2.0-bbbbbbb"]["active"] is True
    assert by_name["0.2.0-aaaaaaa"]["active"] is False
    # The older install is a roll back; the live one is neither.
    assert by_name["0.2.0-aaaaaaa"]["relation"] == "older"
    assert by_name["0.2.0-bbbbbbb"]["relation"] == "active"
    assert by_name["0.2.0-aaaaaaa"]["installed_at"].startswith("2023-11-")
    assert body["newer_available"] is False
    assert "pull" in body["note"].lower() or "download" in body["note"].lower()


def test_an_operator_rolls_back_and_the_stack_restarts(
    client: TestClient, arc_home: Path, restarts: list[list[str]], audit: AuditRecorder
) -> None:
    resp = client.post(
        "/api/maintenance/runtime/activate", json={"version": "0.2.0-aaaaaaa"}, headers=_op()
    )

    assert resp.status_code == 200
    assert resp.json()["restarting"] is True
    assert paths.active_runtime_version() == "0.2.0-aaaaaaa"
    assert len(restarts) == 1
    assert audit.outcomes("runtime.activate") == ["applied"]


def test_a_viewer_cannot_switch_the_runtime(
    client: TestClient, restarts: list[list[str]], audit: AuditRecorder
) -> None:
    resp = client.post(
        "/api/maintenance/runtime/activate", json={"version": "0.2.0-aaaaaaa"}, headers=_viewer()
    )

    assert resp.status_code == 403
    assert paths.active_runtime_version() == "0.2.0-bbbbbbb"
    assert restarts == []
    assert "denied" in audit.outcomes("runtime.activate")


@pytest.mark.parametrize(
    "version",
    ["../state", "..", "a/b", "/etc", "0.2.0 aaa", "0.2.0;rm", "current", "", "x" * 200],
)
def test_only_an_installed_bare_version_name_is_accepted(
    client: TestClient, restarts: list[list[str]], version: str
) -> None:
    """Abuse: aim ``current`` at a path, at ``state/``, or at a name that is not a version."""
    resp = client.post(
        "/api/maintenance/runtime/activate", json={"version": version}, headers=_op()
    )

    assert resp.status_code in (400, 404)
    assert paths.active_runtime_version() == "0.2.0-bbbbbbb"
    assert restarts == []


def test_a_version_that_is_not_installed_is_refused_in_plain_words(
    client: TestClient, restarts: list[list[str]]
) -> None:
    resp = client.post(
        "/api/maintenance/runtime/activate", json={"version": "9.9.9"}, headers=_op()
    )

    assert resp.status_code == 404
    assert "not installed" in resp.json()["error"]
    assert restarts == []


def test_a_restart_that_cannot_start_leaves_the_old_version_live(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(argv: list[str]) -> None:
        raise OSError("no systemd")

    monkeypatch.setattr(stack_routes, "_spawn_restart", _boom)

    resp = client.post(
        "/api/maintenance/runtime/activate", json={"version": "0.2.0-aaaaaaa"}, headers=_op()
    )

    assert resp.status_code == 500
    assert paths.active_runtime_version() == "0.2.0-bbbbbbb"


def test_the_error_text_never_shows_a_command(client: TestClient) -> None:
    resp = client.post(
        "/api/maintenance/runtime/activate", json={"version": "9.9.9"}, headers=_op()
    )

    assert "arc " not in resp.json()["error"]
