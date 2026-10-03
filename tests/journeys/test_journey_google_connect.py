"""Journey J1 G14: Google connect, sync, search over a simulated 8 days with zero operator steps.

The real ``google_workspace`` bundle (native REST attachment + Gmail source adapter),
the real arcui routes (app setup, begin, the SPA-equivalent ``POST /api/oauth/complete``),
real sealed custody and the deployment app slot, the real renewer and two agent
credential brokers racing over one custody row, the real connected-data service and
ArcMemory ingest, and ``document_search``. The only doubles are Google's token
endpoint and Gmail REST (:mod:`packages.arcagent.tests.oauth_fakes`), which honour
PKCE, single-use codes and access-token expiry.
"""

from __future__ import annotations

import asyncio
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

from packages.arcagent.tests.oauth_fakes import FakeGmail, FakeOAuthProvider
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
EMAIL = "josh@blackarcindustrial.com"
INSTANCE = "blackarc"
REDIRECT = "http://127.0.0.1:8420/oauth/callback"
AGENT_DID = "did:arc:agent:acme"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def google_bundle(world: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The real bundle in the deployment's bundle root, its HTTP pointed at the fakes."""
    target = _bundles(world) / "google_workspace"
    shutil.copytree(
        REPO / "extensions" / "google_workspace",
        target,
        ignore=shutil.ignore_patterns("__pycache__", "tests"),
    )
    yield target
    for name in [key for key in sys.modules if key.startswith("arc_ext_google_workspace")]:
        del sys.modules[name]


def _route_gmail(monkeypatch: pytest.MonkeyPatch, bundle: Path, gmail: FakeGmail) -> None:
    monkeypatch.syspath_prepend(str(bundle))
    from arc_ext_google_workspace.native import http as google_http

    transport = gmail.transport()
    monkeypatch.setattr(
        google_http.GoogleHttp,
        "_new_client",
        lambda self: httpx.AsyncClient(transport=transport),
    )


async def _wait(check: Any, *, seconds: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if await check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


async def test_google_connect_sync_search_week_simulated(
    world: Path, google_bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    provider = FakeOAuthProvider(clock=clock)
    gmail = FakeGmail(provider, email=EMAIL)
    gmail.add_message(
        "m1", subject="Quarterly invoice", body="The quarterly invoice is 4711 dollars."
    )
    _route_gmail(monkeypatch, google_bundle, gmail)

    # --- 1. the operator: set up Google sign-in once, add the account, one click --
    client, _agent_id, _dir = _agent(world)
    client.app.state.oauth_token_post = provider.post
    client.app.state.oauth_redirect_uri = REDIRECT
    operator = _headers("operator")

    def setup_and_connect() -> dict[str, Any]:
        assert (
            client.put(
                "/api/oauth-apps/google",
                json={"client_id": provider.client_id, "client_secret": provider.client_secret},
                headers=operator,
            ).status_code
            == 200
        )
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

    row = await asyncio.to_thread(setup_and_connect)
    assert row["status"] == "healthy", row
    operator_actions = 3  # set up the app, add the account, click Connect

    # --- 2. the agent: its attachment reads the access token through its handle ----
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
            refresh=lambda request: refresh_access_token(request, post=provider.post),
            health=StoreHealthReporter(opener),
            owner_id=owner,
            client=apps.client_for,
            state=state,
            clock=clock,
            sleep=no_sleep,
        )

    manifest = load_manifest((google_bundle / "extension.toml").read_text(), tier=Tier.PERSONAL)
    plan = credential_plan(manifest)

    def agent_attachment(process: str) -> Any:
        broker = AccessTokenBroker(
            rows(),
            registry=lambda: ConnectionRegistry(_arc_dir(world)),
            renewals=planner(process),
            health=StoreHealthReporter(opener),
            clock=clock,
            bound_agent=_AGENT,
            bound_did=AGENT_DID,
        )
        handle = broker.handle(INSTANCE, agent=_AGENT, agent_did=AGENT_DID, plan=plan)
        return build_attachment(
            manifest,
            google_bundle,
            {"account": Secret(EMAIL), "read_only": Secret("yes")},
            connection_id=INSTANCE,
            credential=handle,
        )

    agent_a, agent_b = agent_attachment("agent-proc-a"), agent_attachment("agent-proc-b")

    # --- 3. knowledge: sync Gmail into ArcMemory, then search it ---------------------
    approval = ApprovalStore(backend)
    sync_state = InMemorySourceSyncStore()
    catalog = SourceCatalog()
    await catalog.register(INSTANCE, agent_a.source_adapter())
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
    proposal = await service.stage_mapping(INSTANCE, homes=(KnowledgeHome.DOCUMENT,))
    assert proposal is not None
    await approval.resolve(
        proposal.approval_id,
        status="approved",
        actor_did="did:arc:operator",
        resolved_by="did:arc:operator",
    )
    operator_actions += 1  # approving what Gmail maps into knowledge
    assert (await service.sync_now(INSTANCE)).status == "scheduled"
    await _wait(lambda: status_is("complete"))

    from arcmemory.brain import ArcMemoryBrain

    source_id = ingest.canonical_source_id(
        SourceDescription(connection_id=INSTANCE, source_kind="gmail", account_id="gmail-account")
    )
    brain = ArcMemoryBrain(world / "workspace", AGENT_DID)
    hits = await brain.document_search(
        "quarterly invoice", source_id=source_id, caller_did=AGENT_DID
    )
    assert hits and "4711" in hits[0].text

    # --- 4. eight days, 15-minute steps, nobody touches it --------------------------
    renewer = planner("arcui-renewer")
    refreshes_before = provider.refreshes
    actions_at_connect = operator_actions
    for step in range(8 * 24 * 4):
        jobs: list[Any] = [renewer.ensure_fresh(INSTANCE, flow=manifest.oauth)]
        if step % 4 == 0:  # each agent process makes a real Gmail call every hour
            jobs += [
                agent_a.invoke("google_gmail_labels", {}),
                agent_b.invoke("google_gmail_labels", {}),
            ]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        for result in results:
            assert not isinstance(result, BaseException), result
        for tool_result in results[1:]:
            assert str(tool_result.outcome) == "ok", tool_result.content
        record = await state.get(INSTANCE)
        assert record is not None and record.status == "healthy", record
        clock.now += timedelta(minutes=15)

    renewals = provider.refreshes - refreshes_before
    assert renewals >= 3
    # One renewal per due window (75 % of a one-hour token), never one per process.
    assert renewals <= (8 * 24 * 60) // 45 + 2, renewals
    assert gmail.rejected == 0, "a tool call presented an expired or unknown token"
    access_seen = provider.issued_access
    assert len(access_seen) == len(set(access_seen))
    record = await state.get(INSTANCE)
    assert record is not None and record.status == "healthy" and record.notice_seq == 0
    assert operator_actions == actions_at_connect, "the week needed an operator"

    # --- 5. search still answers, and a fresh sync still reaches Gmail ----------------
    gmail.add_message("m2", subject="Renewal notice", body="The renewal is due on Friday.")
    assert (await service.sync_now(INSTANCE)).status == "scheduled"

    async def found_new() -> bool:
        found = await brain.document_search(
            "renewal due", source_id=source_id, caller_did=AGENT_DID
        )
        return bool(found)

    await _wait(found_new)
    assert await brain.document_search(
        "quarterly invoice", source_id=source_id, caller_did=AGENT_DID
    )
    await service.close()
