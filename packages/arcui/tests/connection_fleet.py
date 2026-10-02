"""A one-agent fleet with one real bundle, the connector routes and an in-memory arcstore.

Shared by the connection-health tests (card API, probe loop, abuse cases). Real
routes and a real arcstore; only the provider wire is the bundle's own fake adapter.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path
from typing import Any

import arcagent
import pytest
from arcgateway import team_roster
from arcstore.backends.memory import FakeBackend
from arcstore.source_sync import ArcStoreSourceSyncStore
from arctrust.audit import AuditEvent
from arctrust.identity import AgentIdentity
from arctrust.paths import arc_team
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.connectors import routes as connector_routes

EXTENSION = "acme_card"
INSTANCE = "work"
AGENT = "acme_agent"
SENTINEL = "zzz-card-sentinel-8842"

_MANIFEST = """
[extension]
name = "acme_card"
version = "1.0.0"
display_name = "Acme"
attachment = "native"

[config.native]
entrypoint = "acme_attachment"

[[secrets]]
name = "api_token"
prompt = "Acme API token."

[health]
probe = "attachment"

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "Ping Acme."
classification = "read_only"
"""

_ADAPTER = """
from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec


class Acme:
    def __init__(self, context: dict[str, Any]) -> None:
        self._credential = context.get("credential")

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        token = await self._credential.maybe_field("api_token") if self._credential else None
        return ProbeResult(
            reachable=bool(token and token.reveal()),
            tools=await self.describe_tools(),
            detail="acme says no",
        )

    async def describe_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="ping", description="Ping Acme.", classification="read_only")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="pong")


def build_native_attachment(context: dict[str, Any]) -> Acme:
    return Acme(context)
"""


class Recording:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class Fleet:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
        # ARC_TEAM_ROOT outranks ARC_CONFIG_DIR, and the adversarial battery exports it.
        monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc"))
        monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
        # Code-bearing bundles never load from the operator tree (P18-2); the fleet's
        # bundle lives on the explicit extensions root, as a shipped bundle would.
        monkeypatch.setenv("ARC_EXTENSIONS_ROOT", str(tmp_path / "bundles"))
        arc_dir = tmp_path / "arc"
        bundle = tmp_path / "bundles" / EXTENSION
        bundle.mkdir(parents=True)
        (bundle / "extension.toml").write_text(_MANIFEST, encoding="utf-8")
        (bundle / "acme_attachment.py").write_text(_ADAPTER, encoding="utf-8")

        identity = AgentIdentity.generate(org="arc", agent_type="exec")
        key_dir = tmp_path / "keys"
        identity.save_keys(key_dir)
        self.did = identity.did
        team_root = arc_team(base=arc_dir)
        agent_dir = team_root / AGENT
        (agent_dir / "workspace").mkdir(parents=True)
        (agent_dir / "arcagent.toml").write_text(
            '[agent]\nname = "acme"\norg = "arc"\ntype = "exec"\n'
            f'workspace = "{agent_dir / "workspace"}"\n'
            '[security]\ntier = "personal"\n'
            f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
            encoding="utf-8",
        )
        self._team_root = team_root
        self.backend = FakeBackend()
        self.sink = Recording()
        self.client = self._build_client()

    def _build_client(self) -> TestClient:
        """A fresh app over the SAME arcstore: what a process restart leaves behind."""
        auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = Starlette(routes=connector_routes)
        app.add_middleware(AuthMiddleware, auth_config=auth)
        app.state.auth_config = auth
        team_root = self._team_root
        app.state.roster_provider = lambda: team_roster.list_team(
            team_root=team_root, online_ids=set()
        )
        app.state.arcstore_backend = self.backend
        app.state.audit_worm = types.SimpleNamespace(
            sink=self.sink, operator_did="did:arc:op", write=lambda fields: None
        )
        return TestClient(app)

    def restart(self) -> None:
        """Tear the app down and build a new one on the same arcstore and filesystem."""
        self.client.close()
        self.client = self._build_client()

    def headers(self, token: str = "operator") -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def install(self) -> None:
        response = self.client.post(
            "/api/connections",
            json={
                "extension": EXTENSION,
                "instance": INSTANCE,
                "agents": [AGENT],
                "secrets": {"api_token": SENTINEL},
            },
            headers=self.headers(),
        )
        assert response.status_code == 200, response.text

    def listing(self) -> list[dict[str, Any]]:
        response = self.client.get("/api/connections", headers=self.headers("viewer"))
        assert response.status_code == 200, response.text
        rows: list[dict[str, Any]] = response.json()["connections"]
        return rows

    def authority(self) -> arcagent.ConnectionHealthAuthority:
        return arcagent.ConnectionHealthAuthority(arcagent.ConnectionStateStore(self.backend))

    def report(self, *, ok: bool, code: str = "invalid_grant") -> None:
        signal = arcagent.HealthSignal(
            ok=ok,
            source="probe",
            checked_by=arcagent.PROBE_DID,
            reason_code=None if ok else code,  # type: ignore[arg-type] # reason: test literal
            provider="Acme",
        )
        assert asyncio.run(self.authority().record(INSTANCE, signal)) is not None

    def sync(self, agent_did: str, source_id: str, *, ttl: float) -> None:
        store = ArcStoreSourceSyncStore(self.backend)
        assert asyncio.run(store.acquire_lease(agent_did, source_id, "w", ttl_seconds=ttl))


__all__ = ["AGENT", "EXTENSION", "INSTANCE", "SENTINEL", "Fleet", "Recording"]
