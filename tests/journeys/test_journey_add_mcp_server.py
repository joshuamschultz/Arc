"""Journey: an operator adds their own MCP server from the web and an agent uses it.

Alpha-2 P12, "where do I add my own MCP servers?". Real everything between the form and the
agent: the arcui routes, the ``Connections`` seam, a real stdio MCP server child process, a
real signed bundle on disk, the real connectors capability and a real tool registry. The only
fake is the arcstore backend (in memory), which is what a restarted process would re-open.

The story, in the operator's order: ask the server what it offers (nothing is written), add
two of its tools, see the connection with Knowledge honestly "not applicable", watch the agent
call a tool under its namespaced name, then edit the bundle behind Arc's back and see the
agent refuse to attach it after the next restart.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcagent.core.config import ToolConfig, ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tier import Tier
from arcagent.core.tool_registry import ToolRegistry
from arcagent.extension.custody_select import deployment_cipher
from arcagent.modules.connectors import _runtime
from arcagent.modules.connectors.capabilities import Connectors
from arcagent.tools.human_gate import HumanGate
from arcgateway import team_roster
from arcstore.backends.memory import FakeBackend
from arctrust import operator_signer_for
from arctrust.identity import AgentIdentity
from arctrust.paths import arc_team
from arctrust.signer import InProcessSigner
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes import connectors as connector_routes
from arcui.routes import mcp_servers as mcp_routes
from nacl.signing import SigningKey
from starlette.applications import Starlette
from starlette.testclient import TestClient

_SERVER = (
    Path(__file__).resolve().parents[2] / "packages/arcagent/tests/fixtures/mcp_stdio_server.py"
)
_AGENT = "journey_agent"
_SECRET = "journey-s3cr3t-8c41"
_OPERATOR = {"Authorization": "Bearer operator"}


async def _open(backend: FakeBackend) -> FakeBackend:
    return backend


@pytest.fixture
def deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Path, FakeBackend]:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc"))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = tmp_path / "keys"
    identity.save_keys(key_dir)
    team_root = arc_team(base=tmp_path / "arc")
    agent_dir = team_root / _AGENT
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        '[agent]\nname = "journey"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n[security]\ntier = "personal"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=[*connector_routes.routes, *mcp_routes.routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    backend = FakeBackend()
    app.state.arcstore_backend = backend
    return TestClient(app), tmp_path, backend


async def _start_agent(root: Path, backend: FakeBackend) -> ToolRegistry:
    """Start the connectors capability the way a restarted agent does."""
    did = "did:arc:arc:exec/journey"
    gate = HumanGate(
        operator_signer=InProcessSigner(bytes(SigningKey.generate())),
        agent_did=did,
        tier="personal",
    )
    registry = ToolRegistry(
        config=ToolsConfig(policy=ToolConfig()),
        bus=ModuleBus(),
        telemetry=MagicMock(),
        human_gate=gate,
    )
    identity: Any = MagicMock()
    identity.did = did
    _runtime.reset()
    _runtime.configure(
        config={"data_dir": str(root / "data"), "arc_dir": str(root / "arc")},
        telemetry=None,
        arcstore_opener=lambda: _open(backend),
        workspace=root / "arc" / "team" / _AGENT / "workspace",
        identity=identity,
        config_path=root / "arc" / "team" / _AGENT / "arcagent.toml",
        tool_registry=registry,
        tier="personal",
        human_gate=gate,
        # The agent opens custody with the same operator-derived key arcui sealed with.
        credential_cipher=deployment_cipher(root / "arc", tier=Tier.PERSONAL),
        # Bundles in the operator tree verify at every tier, against the operator key.
        operator_signer=operator_signer_for(base=root / "arc"),
    )
    await Connectors().setup(None)
    return registry


async def test_operator_adds_a_server_and_the_agent_uses_it_until_the_bundle_is_edited(
    deployment: tuple[TestClient, Path, FakeBackend],
) -> None:
    client, root, backend = deployment
    form: dict[str, Any] = {
        "name": "journey",
        "transport": "stdio",
        "argv": [sys.executable, str(_SERVER)],
        "env_refs": {"api_key": "JOURNEY_API_KEY"},
        "secrets": {"api_key": _SECRET},
    }

    # 1. Discover. The operator sees what the server offers; nothing is written.
    preview = client.post("/api/mcp-servers/preview", json=form, headers=_OPERATOR)
    assert preview.status_code == 200, preview.text
    assert {"echo", "danger_delete"} <= {tool["name"] for tool in preview.json()["tools"]}
    assert not (root / "arc" / "extensions").exists()

    # 2. Add two of the five tools, classified by the operator, for one agent.
    chosen = {
        "echo": {"classification": "read_only", "capability_tags": ["subprocess"]},
        "has_variable": {"classification": "read_only", "capability_tags": ["subprocess"]},
    }
    added = client.post(
        "/api/mcp-servers", json={**form, "tools": chosen, "agents": [_AGENT]}, headers=_OPERATOR
    )
    assert added.status_code == 200, added.text
    assert _SECRET not in added.text

    # 3. The connection exists, and Knowledge honestly says it does not apply.
    listing = client.get("/api/connections", headers=_OPERATOR).json()["connections"]
    card = next(row for row in listing if row["instance"] == "journey")
    assert card["knowledge_mode"] == "non_indexable"
    assert card["agents"] == [_AGENT]

    # 4. The agent serves exactly the chosen tools, namespaced, and a call works.
    registry = await _start_agent(root, backend)
    assert {"journey__echo", "journey__has_variable"} <= set(registry.tools)
    assert "journey__danger_delete" not in registry.tools and "echo" not in registry.tools
    assert "echo: hi" in str(await registry.tools["journey__echo"].execute(text="hi"))
    # The credential reached the child's environment (and nowhere else the agent can read).
    assert "yes" in str(
        await registry.tools["journey__has_variable"].execute(name="JOURNEY_API_KEY")
    )

    # 5. Someone edits the bundle behind Arc's back. After the next restart nothing attaches.
    manifest = root / "arc" / "extensions" / "journey" / "extension.toml"
    manifest.write_text(manifest.read_text().replace("journey__echo", "journey__danger_delete"))
    restarted = await _start_agent(root, backend)
    assert not any(name.startswith("journey__") for name in restarted.tools)
    _runtime.reset()
