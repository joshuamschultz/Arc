"""Journey J1 F7: connect Google in one click, sync Drive, find a Doc by meaning.

The real ``google_workspace`` bundle (native REST attachment, Gmail and Drive source
adapters), the real arcui connect routes, real sealed custody and the access-token
broker, the real connected-data service and ArcMemory ingest, and ``document_search``.
The only doubles are Google's: the token endpoint, Gmail REST and Drive REST.
"""

from __future__ import annotations

import asyncio
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
from arcagent.extension.source import InspectSource
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.extension.state import ConnectionStateStore
from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter, ArcStoreObjectState
from arcagent.modules.connected_data.service import ConnectedDataService
from arcagent.modules.connectors.attachments import build_attachment
from arcstore.approvals import ApprovalStore
from arcstore.source_sync import InMemorySourceSyncStore
from arcui.public_address import PublicAddress

from packages.arcagent.tests.drive_fake import DOC, FOLDER, PDF, FakeDrive
from packages.arcagent.tests.oauth_fakes import FakeGmail, FakeOAuthProvider
from packages.arcui.tests.test_connectors_routes import (
    _AGENT,
    _agent,
    _arc_dir,
    _headers,
    world,
)

from .test_journey_google_connect import (
    AGENT_DID,
    EMAIL,
    INSTANCE,
    Clock,
    _wait,
    google_bundle,
)
from .test_journey_knowledge import _require_local_embedder

__all__ = ["google_bundle", "world"]

WARRANTY = "Our hardware warranty covers parts and labour for twenty-four months after delivery."
QUESTION = "how long is the guarantee on the equipment we bought"


class _LocalEmbedder:
    """The production local embedder behind ArcMemory's ``embed_texts`` seam."""

    def __init__(self) -> None:
        from arcllm import embeddings

        self._local = embeddings.LocalEmbedder(embeddings.DEFAULT_EMBED_MODEL)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        response = await self._local.embed(texts)
        return [list(vector) for vector in response.vectors]


def _route_google(monkeypatch: pytest.MonkeyPatch, bundle: Path, *fakes: Any) -> None:
    """Point the bundle's HTTP at the fakes: Drive on its API host, Gmail on the rest."""
    monkeypatch.syspath_prepend(str(bundle))
    from arc_ext_google_workspace.native import http as google_http

    gmail, drive = fakes

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/drive/v3"):
            return drive.handle(request)
        return gmail._handle(request)

    transport = httpx.MockTransport(route)
    monkeypatch.setattr(
        google_http.GoogleHttp, "_new_client", lambda self: httpx.AsyncClient(transport=transport)
    )


def _connect_google(client: Any, provider: FakeOAuthProvider) -> dict[str, Any]:
    """The operator: set up Google sign-in once, add the account, click Connect."""
    operator = _headers("operator")
    app = client.put(
        "/api/oauth-apps/google",
        json={"client_id": provider.client_id, "client_secret": provider.client_secret},
        headers=operator,
    )
    assert app.status_code == 200
    installed = client.post(
        "/api/connections",
        json={
            "extension": "google_workspace",
            "instance": INSTANCE,
            "agents": [_AGENT],
            "secrets": {"account": EMAIL, "read_only": "yes"},
        },
        headers=operator,
    )
    assert installed.status_code == 200, installed.text
    begun = client.post(f"/api/connections/{INSTANCE}/oauth/begin", json={}, headers=operator)
    assert begun.status_code == 200, begun.text
    landed = provider.consent(begun.json()["authorize_url"], email=EMAIL)
    done = client.post("/api/oauth/complete", json={"redirect_url": landed}, headers=operator)
    assert done.status_code == 200, done.text
    return dict(done.json())


async def test_connect_google_sync_drive_find_a_doc_by_meaning(
    world: Path, google_bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _require_local_embedder()
    clock = Clock()
    provider = FakeOAuthProvider(clock=clock)
    gmail = FakeGmail(provider, email=EMAIL)
    drive = FakeDrive(bearer_ok=provider.access_valid)
    drive.add("folder-1", "Contracts", FOLDER)
    drive.add("doc-warranty", "Warranty terms", DOC, WARRANTY, parents=("folder-1",))
    drive.add("doc-lunch", "Lunch menu", DOC, "Tuesday is tacos and Friday is pizza.")
    drive.add("pdf-1", "Scan.pdf", PDF, b"%PDF-1.4 not really a pdf")
    _route_google(monkeypatch, google_bundle, gmail, drive)

    # --- the operator: one click -------------------------------------------------
    client, _agent_id, _dir = _agent(world)
    client.app.state.oauth_token_post = provider.post
    client.app.state.public_address = PublicAddress(ui_port=8420)
    row = await asyncio.to_thread(_connect_google, client, provider)
    assert row["status"] == "healthy", row

    # --- the agent: its attachment reads the access token through its handle ------
    backend = client.app.state.arcstore_backend
    cipher = deployment_cipher(_arc_dir(world), tier=Tier.PERSONAL)
    state = ConnectionStateStore(backend)
    apps = OAuthAppStore(backend, cipher)

    async def opener() -> Any:
        return backend

    rows = CredentialRowStore(backend, cipher, clock=clock)
    renewals = RenewalPlanner(
        rows=rows,
        refresh=lambda request: refresh_access_token(request, post=provider.post),
        health=StoreHealthReporter(opener),
        owner_id="agent-proc",
        client=apps.client_for,
        state=state,
        clock=clock,
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    manifest = load_manifest((google_bundle / "extension.toml").read_text(), tier=Tier.PERSONAL)
    broker = AccessTokenBroker(
        rows,
        registry=lambda: ConnectionRegistry(_arc_dir(world)),
        renewals=renewals,
        health=StoreHealthReporter(opener),
        clock=clock,
        bound_agent=_AGENT,
        bound_did=AGENT_DID,
    )
    handle = broker.handle(
        INSTANCE, agent=_AGENT, agent_did=AGENT_DID, plan=credential_plan(manifest)
    )
    attachment = build_attachment(
        manifest,
        google_bundle,
        {"account": Secret(EMAIL), "read_only": Secret("yes")},
        connection_id=INSTANCE,
        credential=handle,
    )

    # --- knowledge: Drive is its own source beside Gmail ---------------------------
    adapters = attachment.source_adapters()
    assert set(adapters) == {"", "drive"}
    drive_id = f"{INSTANCE}:drive"
    catalog = SourceCatalog()
    await catalog.register(drive_id, adapters["drive"])
    approval = ApprovalStore(backend)
    embedder = _LocalEmbedder()
    ingest = ArcMemoryIngestAdapter(
        world / "workspace",
        AGENT_DID,
        embedder=embedder,
        approval_store=approval,
        object_state=ArcStoreObjectState(backend, actor_did=AGENT_DID),
    )

    async def open_sync() -> InMemorySourceSyncStore:
        return InMemorySourceSyncStore()

    sync_store = await open_sync()

    async def shared_store() -> InMemorySourceSyncStore:
        return sync_store

    service = ConnectedDataService(
        catalog,
        agent_did=AGENT_DID,
        sync_store_opener=shared_store,
        ingest_factory=lambda _: ingest,
        limits=SyncLimits(max_pages=8, max_bytes=1_000_000),
        global_concurrency=1,
        interval_seconds=3600,
    )
    await service.start()

    async def status_is(*wanted: str) -> bool:
        listed = await service.list_sources()
        return bool(listed) and listed[0].status in wanted

    await _wait(lambda: status_is("awaiting_mapping"))
    proposal = await service.stage_mapping(drive_id, homes=(KnowledgeHome.DOCUMENT,))
    assert proposal is not None
    await approval.resolve(
        proposal.approval_id,
        status="approved",
        actor_did="did:arc:operator",
        resolved_by="did:arc:operator",
    )
    assert (await service.sync_now(drive_id)).status == "scheduled"
    await _wait(lambda: status_is("complete"))

    from arcmemory.brain import ArcMemoryBrain

    description = await adapters["drive"].inspect_source(InspectSource(connection_id=drive_id))
    source_id = ingest.canonical_source_id(description)
    brain = ArcMemoryBrain(world / "workspace", AGENT_DID, embedder=embedder)

    async def ask() -> list[Any]:
        return list(
            await brain.document_search(QUESTION, source_id=source_id, caller_did=AGENT_DID)
        )

    # --- a Doc is found by meaning, with a citation back to Drive ------------------
    hits = await ask()
    assert hits, "nothing was indexed from Drive"
    assert "twenty-four months" in hits[0].text
    assert hits[0].title == "Warranty terms"
    assert hits[0].url == "https://drive.google.com/file/d/doc-warranty/view"
    assert hits[0].source_kind == "google_drive"

    # --- a deleted Doc leaves the index, an edited and a new one arrive -------------
    drive.trash("doc-warranty")
    drive.add("doc-new", "Renewal terms", DOC, "Support renews every spring for one year.")
    assert (await service.sync_now(drive_id)).status == "scheduled"

    async def warranty_gone() -> bool:
        return not any("twenty-four months" in hit.text for hit in await ask())

    await _wait(warranty_gone)

    async def renewal_found() -> bool:
        found = await brain.document_search(
            "when does support renew", source_id=source_id, caller_did=AGENT_DID
        )
        return any("renews every spring" in hit.text for hit in found)

    await _wait(renewal_found)
    await service.close()
