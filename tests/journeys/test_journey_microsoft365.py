"""Journey: Microsoft 365 (GCC moderate) connect → tools → knowledge → silent refresh → revoke.

What Josh does, end to end: set up the Microsoft sign-in app once (client ID,
tenant ID, secret, cloud "Commercial / GCC"), add the mailbox, click Connect, sign
in; then an agent reads his mail with ``list-mail-messages``, Knowledge sync makes a
message searchable by that agent, a token expiry is refreshed with nobody noticing,
and when the refresh token is revoked at Microsoft the card turns needs-attention
and the operator is notified once.

Real: the ``microsoft365`` bundle (native Graph attachment + Outlook source), the
arcui routes (app slot, install, begin, the SPA-equivalent ``POST /api/oauth/complete``),
sealed custody and the deployment app slot, the renewer and the agent credential
broker, the connected-data service, ArcMemory ingest and ``document_search``. Fake:
only Entra's token endpoint and Microsoft Graph (:mod:`packages.arcagent.tests.microsoft_fakes`).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcagent.connected_data import KnowledgeHome, SyncLimits
from arcagent.core.tier import Tier
from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.extension.credential_broker import AccessTokenBroker, credential_plan
from arcagent.extension.credentials import RenewalPlanner
from arcagent.extension.custody import CredentialRowStore
from arcagent.extension.custody_select import deployment_cipher
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.manifest import load_manifest
from arcagent.extension.oauth import refresh_access_token
from arcagent.extension.oauth_apps import OAuthAppStore
from arcagent.extension.secrets import Secret
from arcagent.extension.source import SourceDescription
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.extension.state import ConnectionStateStore
from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter, ArcStoreObjectState
from arcagent.modules.connected_data.service import ConnectedDataService
from arcagent.modules.connectors.attachments import build_attachment
from arcstore.approvals import ApprovalStore
from arcstore.source_sync import InMemorySourceSyncStore
from arcui.public_address import PublicAddress

from packages.arcagent.tests.microsoft_fakes import TENANT, FakeEntra, FakeGraph
from packages.arcui.tests.test_connectors_routes import (
    _AGENT,
    _agent,
    _arc_dir,
    _bundles,
    _headers,
    world,
)

__all__ = ["world"]

REPO = Path(__file__).resolve().parents[2]
UPN = "josh@agency.gov"
INSTANCE = "work_mail"
SOURCE = f"{INSTANCE}:outlook"
REDIRECT = "http://127.0.0.1:8420/oauth/callback"
AGENT_DID = "did:arc:agent:acme"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def microsoft_bundle(world: Path) -> Iterator[Path]:
    target = _bundles(world) / "microsoft365"
    shutil.copytree(
        REPO / "extensions" / "microsoft365",
        target,
        ignore=shutil.ignore_patterns("__pycache__", "tests"),
    )
    yield target
    for name in [key for key in sys.modules if key.startswith("arc_ext_microsoft365")]:
        del sys.modules[name]


def _route_graph(monkeypatch: pytest.MonkeyPatch, bundle: Path, graph: FakeGraph) -> None:
    monkeypatch.syspath_prepend(str(bundle))
    from arc_ext_microsoft365.native import graph as graph_module

    transport = graph.transport()
    monkeypatch.setattr(
        graph_module.GraphClient,
        "_new_client",
        lambda _self: httpx.AsyncClient(transport=transport),
    )


async def _wait(check: Any, *, seconds: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if await check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


async def test_microsoft365_connect_tools_knowledge_refresh_and_revoke(
    world: Path, microsoft_bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    entra = FakeEntra(clock=clock)
    graph = FakeGraph(entra, upn=UPN)
    graph.add_message(
        "m1",
        subject="Contract award",
        body="The contract award ceiling is 9813 dollars.",
        sender="co@agency.gov",
    )
    _route_graph(monkeypatch, microsoft_bundle, graph)

    # --- 1. the operator: app slot once, add the mailbox, one click -----------------
    client, _agent_id, _dir = _agent(world)
    client.app.state.oauth_token_post = entra.post
    client.app.state.public_address = PublicAddress(ui_port=8420)
    operator = _headers("operator")

    def setup_and_connect() -> dict[str, Any]:
        shown = client.get("/api/oauth-apps/microsoft", headers=_headers("viewer")).json()
        assert shown["configured"] is False and shown["tenant_required"] is True
        assert shown["clouds"][0] == {"id": "global", "label": "Commercial / GCC"}
        assert shown["redirect_uri"] == REDIRECT
        saved = client.put(
            "/api/oauth-apps/microsoft",
            json={
                "client_id": entra.client_id,
                "client_secret": entra.client_secret,
                "tenant_id": TENANT,
                "cloud": "global",
            },
            headers=operator,
        )
        assert saved.status_code == 200, saved.text
        installed = client.post(
            "/api/connections",
            json={
                "extension": "microsoft365",
                "instance": INSTANCE,
                "agents": [_AGENT],
                "secrets": {"account": UPN},
            },
            headers=operator,
        )
        assert installed.status_code == 200, installed.text
        begun = client.post(f"/api/connections/{INSTANCE}/oauth/begin", json={}, headers=operator)
        assert begun.status_code == 200, begun.text
        landed = entra.consent(begun.json()["authorize_url"], email=UPN)
        done = client.post("/api/oauth/complete", json={"redirect_url": landed}, headers=operator)
        assert done.status_code == 200, done.text
        return dict(done.json())

    row = await asyncio.to_thread(setup_and_connect)
    assert row["status"] == "healthy", row

    # --- 2. the granted agent: its attachment reads the token through its handle ------
    backend = client.app.state.arcstore_backend
    cipher = deployment_cipher(_arc_dir(world), tier=Tier.PERSONAL)
    state = ConnectionStateStore(backend)
    apps = OAuthAppStore(backend, cipher)

    async def opener() -> Any:
        return backend

    def rows() -> CredentialRowStore:
        return CredentialRowStore(backend, cipher, clock=clock)

    async def no_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    def planner(owner: str) -> RenewalPlanner:
        return RenewalPlanner(
            rows=rows(),
            refresh=lambda request: refresh_access_token(request, post=entra.post),
            health=StoreHealthReporter(opener),
            owner_id=owner,
            client=apps.client_for,
            state=state,
            clock=clock,
            sleep=no_sleep,
        )

    manifest = load_manifest((microsoft_bundle / "extension.toml").read_text(), tier=Tier.PERSONAL)
    handle = AccessTokenBroker(
        rows(),
        registry=lambda: ConnectionRegistry(_arc_dir(world)),
        renewals=planner("agent-proc"),
        health=StoreHealthReporter(opener),
        clock=clock,
        bound_agent=_AGENT,
        bound_did=AGENT_DID,
    ).handle(INSTANCE, agent=_AGENT, agent_did=AGENT_DID, plan=credential_plan(manifest))
    agent = build_attachment(
        manifest,
        microsoft_bundle,
        {"account": Secret(UPN), "tenant_id": Secret(TENANT), "cloud": Secret("global")},
        connection_id=INSTANCE,
        credential=handle,
    )

    mail = await agent.invoke("list-mail-messages", {"top": 10})
    assert str(mail.outcome) == "ok", mail.content
    assert json.loads(mail.content)["items"][0]["id"] == "m1"

    # --- 3. knowledge: Outlook syncs into ArcMemory; the agent can search it ----------
    approval = ApprovalStore(backend)
    sync_state = InMemorySourceSyncStore()
    catalog = SourceCatalog()
    await catalog.register(SOURCE, agent.source_adapters()["outlook"])  # type: ignore[attr-defined]  # multi-source wrapper
    ingest = ArcMemoryIngestAdapter(
        world / "workspace",
        AGENT_DID,
        approval_store=approval,
        object_state=ArcStoreObjectState(backend, actor_did=AGENT_DID),
    )

    async def open_sync() -> InMemorySourceSyncStore:
        return sync_state

    service = ConnectedDataService(
        catalog,
        agent_did=AGENT_DID,
        sync_store_opener=open_sync,
        ingest_factory=lambda _: ingest,
        limits=SyncLimits(max_pages=4, max_bytes=1_000_000),
        global_concurrency=1,
        interval_seconds=3600,
    )
    await service.start()

    async def status_is(*wanted: str) -> bool:
        listed = await service.list_sources()
        return bool(listed) and listed[0].status in wanted

    await _wait(lambda: status_is("awaiting_mapping"))
    proposal = await service.stage_mapping(SOURCE, homes=(KnowledgeHome.DOCUMENT,))
    assert proposal is not None
    await approval.resolve(
        proposal.approval_id,
        status="approved",
        actor_did="did:arc:operator",
        resolved_by="did:arc:operator",
    )
    assert (await service.sync_now(SOURCE)).status == "scheduled"
    await _wait(lambda: status_is("complete"))

    from arcmemory.brain import ArcMemoryBrain

    source_id = ingest.canonical_source_id(
        SourceDescription(
            connection_id=SOURCE, source_kind="outlook", account_id="microsoft365-account"
        )
    )
    brain = ArcMemoryBrain(world / "workspace", AGENT_DID)
    hits = await brain.document_search(
        "contract award ceiling", source_id=source_id, caller_did=AGENT_DID
    )
    assert hits and "9813" in hits[0].text
    await service.close()

    # --- 4. the access token expires: the next call refreshes silently ---------------
    refreshes_before = entra.refreshes
    clock.now += timedelta(hours=2)
    again = await agent.invoke("list-mail-folders", {})
    assert str(again.outcome) == "ok", again.content
    assert entra.refreshes == refreshes_before + 1
    assert entra.wrong_endpoint_posts == 0 and entra.refresh_scopes_seen
    record = await state.get(INSTANCE)
    assert record is not None and record.status == "healthy" and record.notice_seq == 0

    # --- 5. Microsoft revokes the refresh token: needs attention, one notice ----------
    entra.revoke_all()
    clock.now += timedelta(hours=2)
    dead = await agent.invoke("list-mail-folders", {})
    assert str(dead.outcome) == "error"
    for secret in entra.secrets_seen():
        assert secret not in dead.content
    record = await state.get(INSTANCE)
    assert record is not None and record.status == "needs_you", record
    # A notice is raised for this transition, and a second failing call raises no other.
    assert record.notice_seq == record.transition_seq > 0
    noticed = record.notice_seq
    await agent.invoke("list-mail-folders", {})
    record = await state.get(INSTANCE)
    assert record is not None and record.notice_seq == noticed
    shown = client.get("/api/connections", headers=_headers("viewer"))
    assert shown.status_code == 200
    assert INSTANCE in shown.text and "needs_you" in shown.text
