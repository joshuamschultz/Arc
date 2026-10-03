"""J1-4: custody repair from the dashboard, and arcui starts when migration refuses.

A refused ``connections.env`` migration used to stop arcui from starting, with a
terminal command as the only way out. Now arcui starts degraded; the operator
sees the unplaceable keys BY NAME ONLY and answers each one (map, keep, drop),
and the migration then runs with its normal read-back proof.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import arcagent
import pytest
from arcagent.extension.custody import CredentialRowStore
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from arctrust.connector_cipher import ConnectorSecretCipher
from arctrust.paths import config_file
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.credential_renewer import migrate_or_degrade
from arcui.routes.custody import routes as custody_routes

REPO_EXTENSIONS = Path(__file__).resolve().parents[3] / "extensions"
CIPHER = ConnectorSecretCipher(b"\x09" * 32)
BODY = (
    "ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-secret-1\n"
    "JIRA_API_TOKEN=jira-orphan-2\n"
    "OLD_NOTE=old-note-3\n"
)
VALUES = ("xoxp-secret-1", "jira-orphan-2", "old-note-3")


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _Box:
    def __init__(self, tmp_path: Path, body: str = BODY) -> None:
        arc_dir = tmp_path / "arc"
        self.backend = FakeBackend()
        self.sink = _Sink()
        backend = self.backend

        async def opener() -> Any:
            return backend

        world = arcagent.resolve_deployment(arc_dir=arc_dir, extensions_root=REPO_EXTENSIONS)
        self.connections = arcagent.Connections(
            world,
            audit=arcagent.AuditChain.held(self.sink),
            state_opener=opener,
            credential_cipher=CIPHER,
        )
        self.connections.registry.define(
            "work_slack", arcagent.Connection(extension="slack", approval="auto", agents=())
        )
        self.env = config_file("connections.env", arc_dir)
        self.env.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.env), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, body.encode())
        os.close(fd)
        self.auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = Starlette(routes=custody_routes)
        app.add_middleware(AuthMiddleware, auth_config=self.auth)
        app.state.auth_config = self.auth
        app.state.audit = UIAuditLogger(enabled=False)
        app.state.credential_connections = lambda: self.connections
        self.client = TestClient(app)

    def operator(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.operator_token}"}

    def viewer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.viewer_token}"}


@pytest.fixture
def box(tmp_path: Path) -> _Box:
    return _Box(tmp_path)


def _answers(*, drop_confirm: str = "JIRA_API_TOKEN") -> dict[str, Any]:
    return {
        "decisions": [
            {"key": "JIRA_API_TOKEN", "action": "drop", "confirm": drop_confirm},
            {"key": "OLD_NOTE", "action": "keep"},
        ]
    }


def test_the_status_names_each_key_and_never_a_value(box: _Box) -> None:
    response = box.client.get("/api/custody", headers=box.operator())

    body = response.json()
    assert response.status_code == 200
    assert body["state"] == "needs_review"
    assert {k["key"]: k["reason"] for k in body["keys"]} == {
        "JIRA_API_TOKEN": "undeclared",
        "OLD_NOTE": "undeclared",
    }
    assert {"connection": "work_slack", "field": "user_token"} in body["targets"]
    assert body["affected_connections"] == ["work_slack"]
    assert not any(value in response.text for value in VALUES)


def test_a_viewer_cannot_read_or_resolve(box: _Box) -> None:
    assert box.client.get("/api/custody", headers=box.viewer()).status_code == 403
    denied = box.client.post("/api/custody/resolve", json=_answers(), headers=box.viewer())
    assert denied.status_code == 403
    assert box.env.read_text() == BODY


def test_drop_needs_the_key_typed_back_exactly(box: _Box) -> None:
    response = box.client.post(
        "/api/custody/resolve",
        json=_answers(drop_confirm="jira_api_token"),
        headers=box.operator(),
    )

    assert response.status_code == 400
    assert response.json()["error"] == "drop_confirmation_mismatch"
    assert box.env.read_text() == BODY


def test_every_key_answered_runs_the_migration_and_leaves_only_what_was_kept(box: _Box) -> None:
    response = box.client.post("/api/custody/resolve", json=_answers(), headers=box.operator())

    body = response.json()
    assert response.status_code == 200
    assert body["state"] == "ok"
    assert box.env.read_text() == "OLD_NOTE=old-note-3\n"
    assert body["result"]["migrated"] == ["work_slack/user_token"]
    assert body["result"]["dropped"] == ["JIRA_API_TOKEN"]
    assert not any(value in response.text for value in VALUES)
    assert [e.extra["key"] for e in box.sink.events if e.action.endswith(".dropped")] == [
        "JIRA_API_TOKEN"
    ]


def test_a_value_can_be_mapped_onto_a_declared_field(tmp_path: Path) -> None:
    box = _Box(tmp_path, body="OLD_SLACK_TOKEN=xoxp-moved-4\n")
    answer = {
        "decisions": [
            {
                "key": "OLD_SLACK_TOKEN",
                "action": "map",
                "connection": "work_slack",
                "field": "user_token",
            }
        ]
    }

    response = box.client.post("/api/custody/resolve", json=answer, headers=box.operator())

    assert response.status_code == 200
    assert not box.env.exists()
    import asyncio

    rows = CredentialRowStore(box.backend, CIPHER)
    row = asyncio.run(rows.read("work_slack"))
    assert row is not None
    assert asyncio.run(rows.open_field(row, "user_token")).reveal() == "xoxp-moved-4"  # type: ignore[union-attr]
    assert "xoxp-moved-4" not in response.text


def test_an_unanswered_key_still_refuses_and_changes_nothing(box: _Box) -> None:
    partial = {"decisions": [{"key": "OLD_NOTE", "action": "keep"}]}

    response = box.client.post("/api/custody/resolve", json=partial, headers=box.operator())

    assert response.status_code == 409
    assert response.json()["error"] == "MIGRATION_UNDECLARED_KEYS"
    assert response.json()["keys"] == ["JIRA_API_TOKEN"]
    assert box.env.read_text() == BODY
    assert not any(value in response.text for value in VALUES)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"decisions": "x"},
        {"decisions": [{"key": "A", "action": "explode"}]},
        {"decisions": [{"key": "A", "action": "map"}]},
        {"decisions": [{"action": "keep"}]},
    ],
)
def test_a_malformed_answer_is_a_client_error(box: _Box, bad: dict[str, Any]) -> None:
    response = box.client.post("/api/custody/resolve", json=bad, headers=box.operator())

    assert response.status_code == 400
    assert box.env.read_text() == BODY


def test_no_file_means_nothing_to_review(tmp_path: Path) -> None:
    box = _Box(tmp_path)
    box.env.unlink()

    body = box.client.get("/api/custody", headers=box.operator()).json()

    assert body["state"] == "ok" and body["keys"] == []


async def test_startup_degrades_instead_of_refusing_to_start(tmp_path: Path) -> None:
    box = _Box(tmp_path)

    assert await migrate_or_degrade(box.connections) is False
    assert box.env.read_text() == BODY


async def test_startup_with_a_clean_file_migrates_and_reports_healthy(tmp_path: Path) -> None:
    box = _Box(tmp_path, body="ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-secret-1\n")

    assert await migrate_or_degrade(box.connections) is True
    assert not box.env.exists()
