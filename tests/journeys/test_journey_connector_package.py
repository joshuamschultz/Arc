"""Journey: an operator adds a third-party connector package from the browser.

UJ-6. Real everything between the drop zone and the agent: the arcui routes (bundle upload,
review, approve; catalog; connect; approve tools), the ``Connections`` seam, the operator
signer, a real signed bundle in ``~/.arc/extensions``, the real connectors capability and a
real tool registry. The only fake is the arcstore backend (in memory), which is what a
restarted process would re-open.

The story, in the operator's order: upload the package, read the review, approve and sign
it, see it in the catalog, connect it with a token for one agent, and watch the agent call
its tool. Then upload an update that changes the tool: the review shows the change, and
after approval the changed tool waits for "Approve new or changed tools" before the agent
can call it again.
"""

from __future__ import annotations

import io
import sys
import zipfile
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
from arcui.routes import connector_bundles as bundle_routes
from arcui.routes import connectors as connector_routes
from nacl.signing import SigningKey
from starlette.applications import Starlette
from starlette.testclient import TestClient

_FIXTURE = Path(__file__).resolve().parents[2] / "packages/arcagent/tests/fixtures/notes_connector"
_AGENT = "journey_agent"
_TOKEN = "journey-notes-token-51b2"
_OPERATOR = {"Authorization": "Bearer operator"}


def _package(**replace: str) -> bytes:
    """The fixture connector zipped the way a person would, inside one folder."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(_FIXTURE.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            data = path.read_bytes()
            for old, new in replace.items():
                data = data.replace(old.encode(), new.encode())
            archive.writestr(f"notes-package/{path.relative_to(_FIXTURE).as_posix()}", data)
    return buffer.getvalue()


async def _open(backend: FakeBackend) -> FakeBackend:
    return backend


@pytest.fixture
def deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Path, FakeBackend]:
    # The real split: the install home (~/.arc) is not the operator tree (~/arc).
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc"))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "dot-arc"))
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
    app = Starlette(routes=[*connector_routes.routes, *bundle_routes.routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    backend = FakeBackend()
    app.state.arcstore_backend = backend
    return TestClient(app), tmp_path, backend


async def _start_agent(root: Path, backend: FakeBackend) -> ToolRegistry:
    """Start the connectors capability the way a restarted agent does.

    A restart is a new process, so the package's module is not already imported.
    """
    sys.modules.pop("arc_ext_notes", None)
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
        credential_cipher=deployment_cipher(root / "arc", tier=Tier.PERSONAL),
        operator_signer=operator_signer_for(base=root / "arc"),
    )
    await Connectors().setup(None)
    return registry


def _upload(client: TestClient, data: bytes) -> dict[str, Any]:
    response = client.post(
        "/api/connector-bundles/upload",
        files={"file": ("notes-package.zip", data, "application/zip")},
        headers=_OPERATOR,
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _approve(client: TestClient, staging_id: str) -> None:
    response = client.post(
        f"/api/connector-bundles/{staging_id}/approve",
        json={"confirm_name": "notes"},
        headers=_OPERATOR,
    )
    assert response.status_code == 200, response.text


async def test_operator_uploads_signs_connects_and_the_agent_uses_it(
    deployment: tuple[TestClient, Path, FakeBackend],
) -> None:
    client, root, backend = deployment

    # 1. Upload. The review names what the package will do; nothing is installed yet.
    staged = _upload(client, _package())
    review = staged["review"]
    assert (review["name"], review["version"], review["attachment"]) == (
        "notes",
        "1.0.0",
        "native",
    )
    assert review["publisher"]["status"] == "unsigned" and review["confirm_required"]
    assert [tool["name"] for tool in review["tools"]] == ["notes_echo"]
    assert [secret["name"] for secret in review["secrets"]] == ["api_token"]
    assert "This package runs its own code inside Arc." in review["flags"]
    catalog = client.get("/api/connectors/catalog", headers=_OPERATOR).json()
    assert "notes" not in {entry["name"] for entry in catalog["available"]}

    # 2. Approve and sign. It appears in the catalog, ready to connect.
    _approve(client, staged["staging_id"])
    catalog = client.get("/api/connectors/catalog", headers=_OPERATOR).json()
    assert "notes" in {entry["name"] for entry in catalog["available"]}

    # 3. Add → Connect with a token, for one agent.
    added = client.post(
        "/api/connections",
        json={
            "extension": "notes",
            "instance": "notes",
            "secrets": {"api_token": _TOKEN},
            "agents": [_AGENT],
        },
        headers=_OPERATOR,
    )
    assert added.status_code == 200, added.text
    assert _TOKEN not in added.text

    # 4. The agent serves the tool, and a call reaches the package's code with the token.
    registry = await _start_agent(root, backend)
    tool = next(name for name in registry.tools if name.endswith("notes_echo"))
    assert "note: hello (token ok)" in str(await registry.tools[tool].execute(text="hello"))

    # 5. The package is in use, so it cannot be removed.
    in_use = client.delete("/api/connector-bundles/notes", headers=_OPERATOR)
    assert in_use.status_code == 409 and in_use.json()["used_by"] == ["notes"]

    # 6. An update that changes the tool. The review shows the change.
    changed = "Echo a note back from the Notes service, now with more words."
    update = _upload(
        client,
        _package(**{"Echo a note back from the Notes service.": changed, "1.0.0": "1.1.0"}),
    )
    assert update["review"]["update"]["tools_changed"] == ["notes_echo"]
    assert update["review"]["update"]["installed_version"] == "1.0.0"
    _approve(client, update["staging_id"])

    # 7. The changed tool waits for the operator; approving it brings it back.
    restarted = await _start_agent(root, backend)
    assert not any(name.endswith("notes_echo") for name in restarted.tools)
    approved = client.post("/api/connections/notes/approve", headers=_OPERATOR)
    assert approved.status_code == 200, approved.text
    again = await _start_agent(root, backend)
    tool = next(name for name in again.tools if name.endswith("notes_echo"))
    assert "note: again (token ok)" in str(await again.tools[tool].execute(text="again"))
    _runtime.reset()
