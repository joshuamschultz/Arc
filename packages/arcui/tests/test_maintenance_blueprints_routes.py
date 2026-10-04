"""Settings -> Maintenance -> Blueprints: list, preview and build an agent from one.

The dashboard builds the agent with :func:`arcagent.scaffold.create_agent` and then
materializes the blueprint through the same signed path ``arc init --blueprint``
uses: persona, operator-signed prompt overlays, operator-signed skills, seeded
schedules. Only an operator can build; a blueprint is chosen from the list, never
named by a path.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import arctrust
import pytest
from arctrust.operator import OperatorKey
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

OP_TOKEN = "o" * 64
VIEW_TOKEN = "v" * 64

_SKILL = (
    "---\nname: deal-review\ndescription: review a deal thoroughly for risk and next steps\n"
    "---\n\n## Files\n\n## Contract\n\n## Knowledge\n\n## Steps\ndo x\n## Output\n\n"
    "## Red Flags & Rationalizations\n\n## Validation\n\n## Examples\n"
)


class FakeRegistry:
    def __init__(self) -> None:
        self.entities: dict[str, Any] = {}

    async def get(self, ref: str) -> Any:
        return self.entities.get(ref)

    async def register(self, entity: Any) -> None:
        self.entities[entity.did] = entity


class AuditRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def audit_event(self, name: str, fields: dict[str, Any]) -> None:
        self.events.append(dict(fields))

    def outcomes(self, operation: str) -> list[str]:
        return [e["outcome"] for e in self.events if e.get("operation") == operation]


def _write_blueprint(root: Path, name: str = "sales") -> None:
    folder = root / name
    (folder / "prompts" / "arcmemory").mkdir(parents=True)
    (folder / "skills" / "deal-review").mkdir(parents=True)
    (folder / "blueprint.toml").write_text(
        "[blueprint]\n"
        f'name = "{name}"\nversion = "1.0.0"\ntier = "personal"\n'
        'description = "Pipeline memory for a sales executive."\n\n'
        "[modules.memory]\nenabled = true\n\n"
        "[[schedules]]\n"
        'type = "cron"\nexpression = "0 8 * * *"\n'
        'prompt = "Give me the morning pipeline briefing."\n',
        encoding="utf-8",
    )
    (folder / "persona.md").write_text("You are a sales chief of staff.\n", encoding="utf-8")
    (folder / "prompts" / "arcmemory" / "distill_fact.md").write_text(
        "Extract contacts, companies, and deals.\n", encoding="utf-8"
    )
    (folder / "skills" / "deal-review" / "SKILL.md").write_text(_SKILL, encoding="utf-8")


@pytest.fixture
def operator_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OperatorKey:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    blueprints = tmp_path / "blueprints"
    _write_blueprint(blueprints)
    monkeypatch.setenv("ARC_BLUEPRINTS_DIR", str(blueprints))
    path = arctrust.default_operator_key_path()
    path.parent.mkdir(parents=True)
    key = OperatorKey.generate()
    key.save(path)
    return key


@pytest.fixture
def team_root(tmp_path: Path) -> Path:
    root = tmp_path / "team"
    root.mkdir()
    return root


@pytest.fixture
def audit() -> AuditRecorder:
    return AuditRecorder()


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def client(
    team_root: Path, operator_key: OperatorKey, audit: AuditRecorder, registry: FakeRegistry
) -> TestClient:
    app = create_app(
        auth_config=AuthConfig({"viewer_token": VIEW_TOKEN, "operator_token": OP_TOKEN}),
        team_root=team_root,
        operator_signer_factory=lambda: operator_key.into_signer(),
    )
    app.state.audit = audit
    app.state.messaging_registry = registry
    return TestClient(app)


def _op() -> dict[str, str]:
    return {"Authorization": f"Bearer {OP_TOKEN}"}


def _viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEW_TOKEN}"}


def test_a_viewer_sees_each_blueprint_and_what_it_would_create(client: TestClient) -> None:
    resp = client.get("/api/maintenance/blueprints", headers=_viewer())

    assert resp.status_code == 200
    item = next(b for b in resp.json()["blueprints"] if b["id"] == "sales")
    assert item["description"] == "Pipeline memory for a sales executive."
    assert item["version"] == "1.0.0"
    assert item["source"] == "packaged"
    creates = item["creates"]
    assert creates["persona"] is True
    assert creates["prompts"] == ["arcmemory/distill_fact"]
    assert creates["skills"] == ["deal-review"]
    assert creates["schedules"] == 1
    assert creates["modules"] == ["memory"]


def test_an_operator_builds_a_signed_registered_agent_from_a_blueprint(
    client: TestClient, team_root: Path, registry: FakeRegistry, audit: AuditRecorder
) -> None:
    resp = client.post(
        "/api/maintenance/blueprints/sales/create",
        json={"agent_name": "closer"},
        headers=_op(),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["agent_id"] == "closer"
    assert body["team_registered"] is True
    assert body["created"] == {
        "persona": True,
        "prompts": 1,
        "capabilities": 0,
        "skills": 1,
        "schedules": 1,
    }
    agent = team_root / "closer"
    assert "chief of staff" in (agent / "workspace" / "identity.md").read_text()
    overlay = agent / "context" / "arcmemory" / "distill_fact.md"
    assert overlay.is_file()
    assert Path(f"{overlay}.arcsig").is_file()
    assert tomllib.loads((agent / "arcagent.toml").read_text())["modules"]["memory"]["enabled"]
    assert len(registry.entities) == 1
    assert audit.outcomes("blueprint.create") == ["applied"]


def test_a_viewer_cannot_build_an_agent(
    client: TestClient, team_root: Path, audit: AuditRecorder
) -> None:
    resp = client.post(
        "/api/maintenance/blueprints/sales/create",
        json={"agent_name": "closer"},
        headers=_viewer(),
    )

    assert resp.status_code == 403
    assert list(team_root.iterdir()) == []
    assert audit.outcomes("blueprint.create") == ["denied"]


@pytest.mark.parametrize(
    "name", ["..", "nope", "~", ".sales", "..%2Fsales", "%2Fetc%2Fpasswd", "sales%00", "x" * 200]
)
def test_a_blueprint_is_chosen_from_the_list_never_by_path(
    client: TestClient, team_root: Path, name: str
) -> None:
    """Abuse: aim the resolver at a folder of the caller's choosing."""
    resp = client.post(
        f"/api/maintenance/blueprints/{name}/create",
        json={"agent_name": "closer"},
        headers=_op(),
    )

    assert resp.status_code in (400, 404, 405)
    assert list(team_root.iterdir()) == []


@pytest.mark.parametrize("name", ["../evil", "a/b", "Helper", "x", ".."])
def test_an_unsafe_agent_name_is_refused(client: TestClient, team_root: Path, name: str) -> None:
    resp = client.post(
        "/api/maintenance/blueprints/sales/create", json={"agent_name": name}, headers=_op()
    )

    assert resp.status_code == 400
    assert list(team_root.iterdir()) == []
    assert "arc " not in resp.json()["error"]


def test_an_existing_agent_is_never_overwritten(client: TestClient, team_root: Path) -> None:
    first = client.post(
        "/api/maintenance/blueprints/sales/create", json={"agent_name": "closer"}, headers=_op()
    )
    identity = (team_root / "closer" / "workspace" / "identity.md").read_text()

    again = client.post(
        "/api/maintenance/blueprints/sales/create", json={"agent_name": "closer"}, headers=_op()
    )

    assert first.status_code == 201
    assert again.status_code == 409
    assert (team_root / "closer" / "workspace" / "identity.md").read_text() == identity


def test_a_blueprint_that_cannot_be_applied_leaves_no_half_built_agent(
    client: TestClient, team_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "broken-blueprints"
    _write_blueprint(broken, "broken")
    (broken / "broken" / "prompts" / "arcmemory" / "no_such_prompt.md").write_text("x\n")
    monkeypatch.setenv("ARC_BLUEPRINTS_DIR", str(broken))

    resp = client.post(
        "/api/maintenance/blueprints/broken/create", json={"agent_name": "closer"}, headers=_op()
    )

    assert resp.status_code in (400, 404, 409)
    assert list(team_root.iterdir()) == []


def test_messaging_offline_still_builds_the_agent_and_says_so(
    team_root: Path, operator_key: OperatorKey
) -> None:
    app = create_app(
        auth_config=AuthConfig({"viewer_token": VIEW_TOKEN, "operator_token": OP_TOKEN}),
        team_root=team_root,
        operator_signer_factory=lambda: operator_key.into_signer(),
    )
    app.state.messaging_registry = None
    offline = TestClient(app)

    resp = offline.post(
        "/api/maintenance/blueprints/sales/create", json={"agent_name": "closer"}, headers=_op()
    )

    assert resp.status_code == 201
    assert resp.json()["team_registered"] is False
    assert "team chat" in resp.json()["notice"]
