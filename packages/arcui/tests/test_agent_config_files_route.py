"""`GET/PATCH /api/agents/{id}/config/{file}` — the per-agent config editor.

Reads return each file's top-level TOML sections; PATCH deep-merges one section
and writes it back, preserving comments and refusing a result that would not
re-parse. Mirrors `test_skill_detail_route.py`'s fixture pattern (real Starlette
app + real agent dir + roster provider).
"""

from __future__ import annotations

from pathlib import Path

from arcgateway import team_roster
from arctrust.identity import AgentIdentity
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_routes

_ARCLLM_TOML = (
    "# arcllm config for the agent\n"
    '[defaults]\n'
    'provider = "anthropic"  # keep the comment\n'
    "temperature = 0.7\n"
    '[vault]\n'
    'backend = "env"\n'
    'signing_key = "sk-should-be-redacted-for-viewers-1234"\n'
)
_ARCRUN_TOML = "[loop]\nmax_turns = 20\n"


def _agent(tmp_path: Path) -> tuple[TestClient, str, Path]:
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = tmp_path / "keys"
    identity.save_keys(key_dir)
    team_root = tmp_path / "team"
    agent_dir = team_root / "olivia_agent"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "olivia"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        '[llm]\nmodel = "test/model"\n[security]\ntier = "personal"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )
    (agent_dir / "arcllm.toml").write_text(_ARCLLM_TOML, encoding="utf-8")
    (agent_dir / "arcrun.toml").write_text(_ARCRUN_TOML, encoding="utf-8")

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=agent_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = AgentRegistry()
    app.state.embedded_agent_cache = None
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    return TestClient(app), "olivia", agent_dir


def _get(client: TestClient, agent_id: str, file: str, token: str = "viewer"):
    return client.get(
        f"/api/agents/{agent_id}/config/{file}",
        headers={"Authorization": f"Bearer {token}"},
    )


def _patch(client: TestClient, agent_id: str, file: str, body: dict, token: str = "operator"):
    return client.patch(
        f"/api/agents/{agent_id}/config/{file}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_get_returns_sections(tmp_path: Path) -> None:
    client, agent_id, _ = _agent(tmp_path)
    resp = _get(client, agent_id, "arcrun")
    assert resp.status_code == 200
    body = resp.json()
    assert body["file"] == "arcrun"
    assert body["sections"]["loop"]["max_turns"] == 20
    assert body["mtime"] > 0


def test_operator_sees_secret_viewer_redacted(tmp_path: Path) -> None:
    client, agent_id, _ = _agent(tmp_path)
    operator = _get(client, agent_id, "arcllm", token="operator").json()
    assert operator["sections"]["vault"]["signing_key"].startswith("sk-should")
    viewer = _get(client, agent_id, "arcllm", token="viewer").json()
    assert viewer["sections"]["vault"]["signing_key"] == "***"
    # A non-sensitive key is untouched.
    assert viewer["sections"]["defaults"]["provider"] == "anthropic"


def test_unknown_file_is_404(tmp_path: Path) -> None:
    client, agent_id, _ = _agent(tmp_path)
    resp = _get(client, agent_id, "passwd")
    assert resp.status_code == 404


def test_missing_file_returns_empty_sections(tmp_path: Path) -> None:
    client, agent_id, agent_dir = _agent(tmp_path)
    (agent_dir / "arcrun.toml").unlink()
    resp = _get(client, agent_id, "arcrun")
    assert resp.status_code == 200
    assert resp.json() == {"file": "arcrun", "sections": {}, "mtime": 0.0}


def test_unknown_agent_is_404(tmp_path: Path) -> None:
    client, _agent_id, _ = _agent(tmp_path)
    resp = _get(client, "nobody", "arcrun")
    assert resp.status_code == 404


def test_patch_merges_and_preserves_comments(tmp_path: Path) -> None:
    client, agent_id, agent_dir = _agent(tmp_path)
    resp = _patch(client, agent_id, "arcrun", {"loop": {"max_turns": 42}})
    assert resp.status_code == 200
    assert resp.json()["sections"]["loop"]["max_turns"] == 42

    resp2 = _patch(client, agent_id, "arcllm", {"defaults": {"temperature": 0.1}})
    assert resp2.status_code == 200
    on_disk = (agent_dir / "arcllm.toml").read_text(encoding="utf-8")
    assert "keep the comment" in on_disk  # comment survived the round-trip
    assert "temperature = 0.1" in on_disk
    assert 'provider = "anthropic"' in on_disk  # untouched key preserved


def test_patch_viewer_is_403(tmp_path: Path) -> None:
    client, agent_id, _ = _agent(tmp_path)
    resp = _patch(client, agent_id, "arcrun", {"loop": {"max_turns": 5}}, token="viewer")
    assert resp.status_code == 403


def test_patch_unknown_file_is_404(tmp_path: Path) -> None:
    client, agent_id, _ = _agent(tmp_path)
    resp = _patch(client, agent_id, "passwd", {"x": {"y": 1}})
    assert resp.status_code == 404


def test_patch_missing_file_is_404(tmp_path: Path) -> None:
    client, agent_id, agent_dir = _agent(tmp_path)
    (agent_dir / "arcrun.toml").unlink()
    resp = _patch(client, agent_id, "arcrun", {"loop": {"max_turns": 5}})
    assert resp.status_code == 404


def test_patch_non_object_body_is_400(tmp_path: Path) -> None:
    client, agent_id, _ = _agent(tmp_path)
    resp = client.patch(
        f"/api/agents/{agent_id}/config/arcrun",
        json=[1, 2, 3],
        headers={"Authorization": "Bearer operator"},
    )
    assert resp.status_code == 400
