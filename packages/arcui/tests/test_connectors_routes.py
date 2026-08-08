"""SPEC-064 T-021 — the connector routes: the web makes a real connection.

Every install here goes through the real
:func:`arcagent.modules.connectors.install.install_connector` against a real
bundle on disk. A test that substitutes the install proves only that the route
can call a substitute — and the ordering it would stop checking (signature gate
before attachment, credential rolled back on a failed probe) is the security
property the whole module exists for.

Three refusals get their own tests because each one is a place a defect would be
invisible: a credential in a response body, a host prerequisite silently
installed, and a write that lands before a refusal.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from arcgateway import team_roster
from arctrust.identity import AgentIdentity
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.connectors import routes as connector_routes

#: Distinctive enough that finding it anywhere in a response is proof of a leak.
_SENTINEL = "zzz-web-connector-sentinel-4711"

_EXTENSION = "acme_tickets"
_INSTANCE = "work"

_MANIFEST = """
[extension]
name = "acme_tickets"
version = "2.1.0"
attachment = "cli"
description = "Open and read Acme tickets."

[[secrets]]
name = "api_token"
prompt = "Paste the Acme API token"

[tools]
allow = ["ping"]

[approval]
default = "outbound"

[config.cli]
binary = "python3"
probe_argv = ["--version"]

[[config.cli.commands]]
tool = "ping"
argv = ["--version"]
description = "Report the Acme client version."
classification = "read_only"
"""

_MANIFEST_NEEDS_HOST = (
    _MANIFEST
    + """
[[host_requires]]
name = "definitely_not_installed_xyz"
instruction = "brew install definitely-not-installed-xyz"
"""
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the whole Arc world into ``tmp_path``.

    ``ARC_EXTENSIONS_ROOT`` is cleared so a value in the developer's environment
    cannot add a bundle the assertions do not expect.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)
    return tmp_path


def _agent(tmp_path: Path) -> tuple[TestClient, str, Path]:
    """A one-agent fleet, an app carrying only the connector routes, two tokens."""
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = tmp_path / "keys"
    identity.save_keys(key_dir)
    team_root = tmp_path / "team"
    agent_dir = team_root / "acme_agent"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "extensions").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        '[agent]\nname = "acme"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        '[security]\ntier = "personal"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=connector_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    return TestClient(app), "acme", agent_dir


def _write_bundle(root: Path, name: str = _EXTENSION, manifest: str = _MANIFEST) -> None:
    bundle = root / name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "extension.toml").write_text(manifest, encoding="utf-8")


def _headers(token: str = "operator") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _install(
    client: TestClient,
    agent_id: str,
    *,
    instance: str = _INSTANCE,
    secrets: dict[str, str] | None = None,
    token: str = "operator",
) -> Any:
    return client.post(
        f"/api/agents/{agent_id}/connectors",
        json={
            "extension": _EXTENSION,
            "instance": instance,
            "secrets": {"api_token": _SENTINEL} if secrets is None else secrets,
        },
        headers=_headers(token),
    )


def _instance_blocks(agent_dir: Path) -> dict[str, Any]:
    raw = tomllib.loads((agent_dir / "arcagent.toml").read_text(encoding="utf-8"))
    blocks = raw.get("extensions", {})
    assert isinstance(blocks, dict)
    return blocks


# --- catalog ---------------------------------------------------------------


def test_catalog_lists_a_readable_bundle(world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fleet = world / "fleet_extensions"
    _write_bundle(fleet)
    monkeypatch.setenv("ARC_EXTENSIONS_ROOT", str(fleet))
    client, _agent_id, _dir = _agent(world)

    resp = client.get("/api/connectors/catalog", headers=_headers("viewer"))
    assert resp.status_code == 200
    body = resp.json()
    entry = next(e for e in body["available"] if e["name"] == _EXTENSION)
    assert entry["version"] == "2.1.0"
    assert entry["description"] == "Open and read Acme tickets."
    assert entry["attachment"] == "cli"
    assert entry["approval_default"] == "outbound"
    assert entry["secrets"] == [{"name": "api_token", "prompt": "Paste the Acme API token"}]
    assert entry["root"] == str(fleet)
    assert body["unreadable"] == []


def test_catalog_reports_an_unparseable_bundle_instead_of_500ing(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = world / "fleet_extensions"
    _write_bundle(fleet)
    _write_bundle(fleet, name="brokenbundle", manifest="this is not = valid toml [[[")
    monkeypatch.setenv("ARC_EXTENSIONS_ROOT", str(fleet))
    client, _agent_id, _dir = _agent(world)

    resp = client.get("/api/connectors/catalog", headers=_headers("viewer"))
    assert resp.status_code == 200
    body = resp.json()
    assert [e["name"] for e in body["available"]] == [_EXTENSION]
    broken = next(e for e in body["unreadable"] if e["name"] == "brokenbundle")
    assert broken["reason"]


# --- agent-scoped listing --------------------------------------------------


def test_agent_connectors_is_empty_before_anything_is_installed(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    resp = client.get(f"/api/agents/{agent_id}/connectors", headers=_headers("viewer"))
    assert resp.status_code == 200
    assert resp.json() == {
        "instances": [],
        "extensions_root": str(agent_dir / "extensions"),
    }


def test_agent_connectors_is_404_for_an_unknown_agent(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    resp = client.get("/api/agents/nobody/connectors", headers=_headers("viewer"))
    assert resp.status_code == 404


# --- install ---------------------------------------------------------------


def test_install_goes_through_the_real_path_and_persists(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(agent_dir / "extensions")

    resp = _install(client, agent_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["instance"] == _INSTANCE
    assert body["extension"] == _EXTENSION
    assert body["tools"] == ["ping"]

    blocks = _instance_blocks(agent_dir)
    assert blocks[_INSTANCE] == {"extension": _EXTENSION, "approval": "outbound"}
    env = (agent_dir / "connectors.env").read_text(encoding="utf-8")
    assert _SENTINEL in env, "the credential belongs in the owner-only env file"

    listing = client.get(f"/api/agents/{agent_id}/connectors", headers=_headers("viewer")).json()
    assert listing["instances"] == [
        {"instance": _INSTANCE, "extension": _EXTENSION, "approval": "outbound"}
    ]


def test_the_install_response_carries_no_credential(world: Path) -> None:
    """Asserted on the serialized body — a dict the test built proves nothing."""
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(agent_dir / "extensions")

    resp = _install(client, agent_id)
    assert resp.status_code == 200, resp.text
    assert _SENTINEL not in resp.text
    assert "api_token" not in resp.text


def test_an_unsatisfied_host_prerequisite_is_400_and_writes_nothing(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(agent_dir / "extensions", manifest=_MANIFEST_NEEDS_HOST)

    resp = _install(client, agent_id)
    assert resp.status_code == 400
    body = resp.json()
    assert body["unsatisfied_host"] == [
        {
            "name": "definitely_not_installed_xyz",
            "instruction": "brew install definitely-not-installed-xyz",
        }
    ]
    assert _instance_blocks(agent_dir) == {}
    assert not (agent_dir / "connectors.env").exists()
    assert _SENTINEL not in resp.text


def test_a_missing_declared_secret_is_422_and_writes_nothing(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(agent_dir / "extensions")

    resp = _install(client, agent_id, secrets={})
    assert resp.status_code == 422
    assert "api_token" in resp.json()["error"]
    assert _instance_blocks(agent_dir) == {}
    assert not (agent_dir / "connectors.env").exists()


def test_a_duplicate_instance_is_409(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(agent_dir / "extensions")
    assert _install(client, agent_id).status_code == 200

    resp = _install(client, agent_id)
    assert resp.status_code == 409


def test_an_unknown_extension_is_400(world: Path) -> None:
    client, agent_id, _dir = _agent(world)
    resp = _install(client, agent_id)
    assert resp.status_code == 400
    assert "acme_tickets" in resp.json()["error"]


# --- the rest of the verbs -------------------------------------------------


def test_auth_rotates_and_names_only_the_fields(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(agent_dir / "extensions")
    assert _install(client, agent_id).status_code == 200

    rotated = "zzz-rotated-sentinel-8822"
    resp = client.put(
        f"/api/agents/{agent_id}/connectors/{_INSTANCE}/auth",
        json={"secrets": {"api_token": rotated}},
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"instance": _INSTANCE, "updated": ["api_token"]}
    assert rotated not in resp.text
    env = (agent_dir / "connectors.env").read_text(encoding="utf-8")
    assert rotated in env
    assert _SENTINEL not in env


def test_probe_reports_a_live_connection(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(agent_dir / "extensions")
    assert _install(client, agent_id).status_code == 200

    resp = client.post(f"/api/agents/{agent_id}/connectors/{_INSTANCE}/probe", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is True
    assert [tool["name"] for tool in body["tools"]] == ["ping"]


def test_doctor_reports_the_credential_as_present_without_its_value(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(agent_dir / "extensions")
    assert _install(client, agent_id).status_code == 200

    resp = client.get(
        f"/api/agents/{agent_id}/connectors/{_INSTANCE}/doctor", headers=_headers("viewer")
    )
    assert resp.status_code == 200
    checks = {row["check"]: row for row in resp.json()["checks"]}
    assert checks["api_token"]["status"] == "present"
    assert checks["connection"]["status"] == "reachable"
    assert _SENTINEL not in resp.text


def test_approve_records_the_contract_served_now(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(agent_dir / "extensions")
    assert _install(client, agent_id).status_code == 200

    resp = client.post(
        f"/api/agents/{agent_id}/connectors/{_INSTANCE}/approve", headers=_headers()
    )
    assert resp.status_code == 200
    assert resp.json() == {"instance": _INSTANCE, "approved": ["ping"]}


def test_remove_drops_the_credential_and_the_config_block(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(agent_dir / "extensions")
    assert _install(client, agent_id).status_code == 200

    resp = client.delete(f"/api/agents/{agent_id}/connectors/{_INSTANCE}", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["removed_secrets"] == ["api_token"]
    assert body["removed_config"] is True
    assert _instance_blocks(agent_dir) == {}
    assert _SENTINEL not in (agent_dir / "connectors.env").read_text(encoding="utf-8")


def test_removing_an_instance_that_does_not_exist_is_not_an_error(world: Path) -> None:
    client, agent_id, _dir = _agent(world)
    resp = client.delete(f"/api/agents/{agent_id}/connectors/ghost", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["removed_config"] is False
    assert body["removed_secrets"] == []


# --- the operator gate -----------------------------------------------------


def test_a_viewer_is_refused_every_mutation(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(agent_dir / "extensions")
    viewer = _headers("viewer")

    assert _install(client, agent_id, token="viewer").status_code == 403
    assert (
        client.put(
            f"/api/agents/{agent_id}/connectors/{_INSTANCE}/auth",
            json={"secrets": {"api_token": _SENTINEL}},
            headers=viewer,
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/agents/{agent_id}/connectors/{_INSTANCE}/probe", headers=viewer
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/agents/{agent_id}/connectors/{_INSTANCE}/approve", headers=viewer
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/agents/{agent_id}/connectors/{_INSTANCE}", headers=viewer).status_code
        == 403
    )
    assert _instance_blocks(agent_dir) == {}
    assert not (agent_dir / "connectors.env").exists()


def test_an_oversized_install_body_is_413(world: Path) -> None:
    client, agent_id, agent_dir = _agent(world)
    _write_bundle(agent_dir / "extensions")
    resp = client.post(
        f"/api/agents/{agent_id}/connectors",
        content=b'{"extension": "acme_tickets", "instance": "work", "secrets": {"api_token": "'
        + b"x" * 70_000
        + b'"}}',
        headers={**_headers(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 413
