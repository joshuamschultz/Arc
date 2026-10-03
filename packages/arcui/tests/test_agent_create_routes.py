"""Agents from the browser (J-O2): create, import and register without a terminal.

The dashboard builds an agent through the same ``arcagent.scaffold.create_agent``
``arc agent create`` uses: a minted DID, operator-signed identity.md and
capability, and fleet registration. Only an operator may do it, the name can
never climb out of the fleet root, and nothing is written when signing is
unavailable.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import arcagent
import arctrust
import pytest
from arctrust.operator import OperatorKey
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

OP_TOKEN = "o" * 64
VIEW_TOKEN = "v" * 64
MODEL = "scripted/model"


class FakeRegistry:
    """The arcteam registry surface agent registration touches."""

    def __init__(self) -> None:
        self.entities: dict[str, Any] = {}

    async def get(self, ref: str) -> Any:
        return self.entities.get(ref)

    async def register(self, entity: Any) -> None:
        if entity.did in self.entities:
            raise ValueError(f"Entity already registered: {entity.did}")
        self.entities[entity.did] = entity


class AuditRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def audit_event(self, name: str, fields: dict[str, Any]) -> None:
        self.events.append(dict(fields))

    def operations(self, outcome: str) -> list[str]:
        return [e["operation"] for e in self.events if e.get("outcome") == outcome]


@pytest.fixture
def operator_key(tmp_path, monkeypatch) -> OperatorKey:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    path = arctrust.default_operator_key_path()
    path.parent.mkdir(parents=True)
    key = OperatorKey.generate()
    key.save(path)
    return key


@pytest.fixture
def team_root(tmp_path) -> Path:
    root = tmp_path / "team"
    root.mkdir()
    return root


@pytest.fixture
def audit() -> AuditRecorder:
    return AuditRecorder()


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


def _app(team_root: Path, operator_key: OperatorKey | None, audit, registry) -> TestClient:
    factory = (lambda: operator_key.into_signer()) if operator_key is not None else None
    app = create_app(
        auth_config=AuthConfig({"viewer_token": VIEW_TOKEN, "operator_token": OP_TOKEN}),
        team_root=team_root,
        operator_signer_factory=factory,
    )
    app.state.audit = audit
    app.state.messaging_registry = registry
    return TestClient(app)


@pytest.fixture
def client(team_root, operator_key, audit, registry) -> TestClient:
    return _app(team_root, operator_key, audit, registry)


def _op() -> dict[str, str]:
    return {"Authorization": f"Bearer {OP_TOKEN}"}


def _identity_body(agent_dir: Path) -> str:
    resolver = arcagent.build_prompt_resolver(agent_dir / "arcagent.toml", "personal")
    doc = resolver.resolve_signed_file(
        "workspace", "identity", agent_dir / "workspace" / "identity.md"
    )
    assert doc is not None
    return str(doc.body)


def test_an_operator_creates_a_runnable_signed_registered_agent(
    client, team_root, registry, audit
):
    resp = client.post("/api/agents", json={"name": "helper", "model": MODEL}, headers=_op())

    assert resp.status_code == 201, resp.text
    body = resp.json()
    agent_dir = team_root / "helper"
    config = tomllib.loads((agent_dir / "arcagent.toml").read_text())
    assert body["agent_id"] == "helper"
    assert body["did"] == config["identity"]["did"] != ""
    assert body["team_registered"] is True
    assert "Agent Identity" in _identity_body(agent_dir)
    calc = agent_dir / "capabilities" / "calculator.py"
    from arcagent.capabilities import artifact_signing

    assert artifact_signing.verify_file(calc, calc.read_bytes()) is True
    manifest = artifact_signing.load_signature(calc)
    assert manifest is not None and manifest.signer_did.startswith("did:arc:operator")
    assert tomllib.loads((agent_dir / "arcllm.toml").read_text())["llm"]["model"] == MODEL
    assert body["did"] in registry.entities
    assert "agent.create" in audit.operations("applied")

    roster = client.get("/api/team/roster", headers=_op()).json()
    assert "helper" in [a["agent_id"] for a in roster["agents"]]
    detail = client.get("/api/agents/helper", headers=_op()).json()
    assert detail["team_registered"] is True


def test_a_viewer_cannot_create_an_agent(client, team_root, audit):
    """Abuse: a viewer tries to create an agent."""
    resp = client.post(
        "/api/agents",
        json={"name": "helper"},
        headers={"Authorization": f"Bearer {VIEW_TOKEN}"},
    )
    assert resp.status_code == 403
    assert list(team_root.iterdir()) == []
    assert "agent.create" in audit.operations("denied")


@pytest.mark.parametrize(
    "name",
    ["../evil", "..", "a/b", "/abs", "Helper", "x", "helper.d", "..%2fevil", "a\\b", " helper"],
)
def test_a_path_traversal_or_unsafe_name_is_refused(client, tmp_path, team_root, name):
    """Abuse: agent create with a path-traversal name."""
    before = sorted(p.name for p in tmp_path.iterdir())

    resp = client.post("/api/agents", json={"name": name}, headers=_op())

    assert resp.status_code == 400
    assert "arc " not in resp.json()["error"]
    assert list(team_root.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_a_symlinked_name_cannot_redirect_the_write(client, tmp_path, team_root):
    outside = tmp_path / "outside"
    outside.mkdir()
    (team_root / "helper").symlink_to(outside)

    resp = client.post("/api/agents", json={"name": "helper"}, headers=_op())

    assert resp.status_code in (400, 409)
    assert list(outside.iterdir()) == []


def test_an_existing_agent_is_never_overwritten(client, team_root):
    assert client.post("/api/agents", json={"name": "helper"}, headers=_op()).status_code == 201
    identity = (team_root / "helper" / "workspace" / "identity.md").read_text()

    again = client.post("/api/agents", json={"name": "helper"}, headers=_op())

    assert again.status_code == 409
    assert (team_root / "helper" / "workspace" / "identity.md").read_text() == identity


def test_without_an_operator_signer_nothing_is_written(
    team_root, audit, registry, tmp_path, monkeypatch
):
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    client = _app(team_root, None, audit, registry)

    resp = client.post("/api/agents", json={"name": "helper"}, headers=_op())

    assert resp.status_code == 503
    assert list(team_root.iterdir()) == []


def test_unknown_fields_are_refused(client, team_root):
    resp = client.post("/api/agents", json={"name": "helper", "parent_dir": "/tmp"}, headers=_op())
    assert resp.status_code == 400
    assert list(team_root.iterdir()) == []


def test_import_brings_the_persona_and_signs_it(client, team_root):
    persona = "# Agent Identity\n\nYou are Ada, the bookkeeper.\n"
    resp = client.post(
        "/api/agents/import",
        json={"name": "ada", "model": MODEL, "files": {"identity.md": persona}},
        headers=_op(),
    )

    assert resp.status_code == 201, resp.text
    assert "You are Ada, the bookkeeper." in _identity_body(team_root / "ada")


def test_import_refuses_code_and_unknown_files(client, team_root):
    for files in (
        {"identity.md": "# Me\n", "capabilities/evil.py": "import os"},
        {"identity.md": "# Me\n", "../identity.md": "x"},
        {"policy.md": "only policy"},
    ):
        resp = client.post(
            "/api/agents/import", json={"name": "ada", "files": files}, headers=_op()
        )
        assert resp.status_code == 400, files
    assert list(team_root.iterdir()) == []


def test_register_adds_an_unregistered_agent_to_the_team(client, team_root, registry):
    client.post("/api/agents", json={"name": "helper"}, headers=_op())
    registry.entities.clear()
    assert client.get("/api/agents/helper", headers=_op()).json()["team_registered"] is False

    resp = client.post("/api/agents/helper/register", headers=_op())

    assert resp.status_code == 200, resp.text
    assert resp.json()["team_registered"] is True
    assert client.get("/api/agents/helper", headers=_op()).json()["team_registered"] is True


def test_a_viewer_cannot_register_an_agent(client, registry):
    client.post("/api/agents", json={"name": "helper"}, headers=_op())
    registry.entities.clear()
    resp = client.post(
        "/api/agents/helper/register", headers={"Authorization": f"Bearer {VIEW_TOKEN}"}
    )
    assert resp.status_code == 403
    assert registry.entities == {}


def test_with_team_messaging_offline_the_agent_is_created_and_says_so(
    team_root, operator_key, audit
):
    client = _app(team_root, operator_key, audit, None)

    resp = client.post("/api/agents", json={"name": "helper"}, headers=_op())

    assert resp.status_code == 201
    assert resp.json()["team_registered"] is False
    assert resp.json()["notice"]
    assert "arc " not in resp.json()["notice"]
    assert client.get("/api/agents/helper", headers=_op()).json()["team_registered"] is None
