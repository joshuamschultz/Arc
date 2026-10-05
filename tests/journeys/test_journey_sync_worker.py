"""Journey: connected data syncs in its own process; the agent's process only reads.

One ``arc ui start`` process hosts the dashboard, the gateway and every agent. A
sync used to run there too, and its disk waits and extraction threads stalled
chat, NATS replies and health checks. Now every write to a document store runs
in the sync worker, a child process the main process supervises. Here a real
``ArcAgent`` (real memory and connected-data modules, the real sync service, the
real sync worker child) syncs a scripted provider; only the provider's pages and
the LLM wire are faked.

* the documents are written by the child (another pid) and the agent finds them;
* this process opens every connected store read-only: no write connection at all;
* "Sync now" from the dashboard route reaches the worker;
* a worker killed mid-sync is restarted, and the run resumes from its cursor;
* while the worker ingests a large account, this process's loop stays responsive.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcagent.extension.source import (
    FetchSourceObject,
    SourceContent,
    SourceObject,
    SourceObjectKind,
    SyncSource,
    SyncSourcePage,
)
from arcagent.modules.connected_data.sync_worker import process_supervisor
from arctrust.paths import connected_knowledge_dir
from arcui.loop_lag import LagReport, LoopLagMonitor, summarize

from .conftest import OPERATOR_TOKEN, Deployment, ScriptedLLM
from .test_journey_knowledge import (
    WIKI_PAGES,
    Page,
    Provider,
    _status_of,
    _until,
    approve,
    ask,
    connect,
    grant,
    start_knowledge_agent,
    sync_service,
)
from .test_journey_knowledge import _module_source as _module_source
from .test_journey_knowledge import arcstore as arcstore
from .test_journey_modules import install_modules, start_agent


class _Connections:
    """Every SQLite connection this process opens, by database and mode."""

    def __init__(self) -> None:
        self.opened: list[str] = []

    def under(self, root: Path) -> list[str]:
        prefixes = {str(root), str(root.resolve())}
        return [name for name in self.opened if any(prefix in name for prefix in prefixes)]


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> _Connections:
    seen = _Connections()
    real = sqlite3.connect

    def spy(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        seen.opened.append(str(database))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    return seen


async def _worker_pid() -> int:
    supervisor = process_supervisor()
    await supervisor.ensure_started()
    assert await supervisor.wait_ready(60)
    status = supervisor.status()
    assert status.state == "up" and status.pid is not None, status
    return status.pid


# ---------------------------------------------------------------------------
# The child writes, the agent reads, and this process never writes a store
# ---------------------------------------------------------------------------


async def test_a_granted_connection_syncs_in_the_worker_and_the_agent_finds_it(
    deployment: Deployment,
    enable_modules: Any,
    scripted_llm: ScriptedLLM,
    connections: _Connections,
) -> None:
    agent = await start_knowledge_agent(deployment, enable_modules)
    try:
        await connect(agent, Provider("wiki", "confluence", "Team wiki", list(WIKI_PAGES)))

        worker = await _worker_pid()
        assert worker != os.getpid(), "the sync ran in the agent's own process"
        stores = connected_knowledge_dir()
        assert sorted(stores.glob("*/memory/connected/**/p-*.md")), "nothing was written"

        found = await ask(agent, scripted_llm, "document_search", {"query": "billing portal"})
        assert "in August" in found, found

        opened = connections.under(stores)
        assert opened, "the agent never read the shared store"
        writable = [name for name in opened if "mode=ro" not in name]
        assert writable == [], f"this process opened a store for writing: {writable}"
    finally:
        await agent.shutdown()


# ---------------------------------------------------------------------------
# "Sync now" from the dashboard reaches the worker
# ---------------------------------------------------------------------------


def _dashboard(deployment: Deployment, agent: Any) -> Any:
    from arcgateway import team_roster
    from arcui.auth import AuthConfig, AuthMiddleware
    from arcui.routes import connected_data
    from starlette.applications import Starlette

    from .conftest import VIEWER_TOKEN

    auth = AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": OPERATOR_TOKEN})
    app = Starlette(routes=connected_data.routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.embedded_agent_cache = {agent.did: agent}
    app.state.sync_worker = process_supervisor()
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=deployment.team_root, online_ids={agent.did}
    )
    # In this event loop, as uvicorn serves it: the agent's service lives here.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://dashboard",
        headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
    )


async def test_sync_now_from_the_dashboard_reaches_the_worker(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    agent = await start_knowledge_agent(deployment, enable_modules)
    try:
        wiki = Provider("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
        proposal = await grant(agent, wiki)
        await approve(agent, proposal.approval_id)
        service = sync_service(agent)
        from arcgateway import team_roster

        client = _dashboard(deployment, agent)
        roster = team_roster.list_team(team_root=deployment.team_root, online_ids={agent.did})
        name = next(entry.agent_id for entry in roster if entry.did == agent.did)

        reply = await client.post(f"/api/agents/{name}/knowledge/sync/wiki/sync")
        assert reply.status_code == 200, reply.text

        async def complete() -> bool:
            return await _status_of(service, "wiki") == "complete"

        assert await _until(complete), await service.list_sources()
        status = (await client.get("/api/knowledge/sync-worker")).json()
        await client.aclose()
        assert status["state"] == "up" and status["pid"] not in (None, os.getpid())
        found = await ask(agent, scripted_llm, "document_search", {"query": "laptop buddy"})
        assert "week one" in found, found
    finally:
        await agent.shutdown()


# ---------------------------------------------------------------------------
# A killed worker is restarted and the run resumes from its cursor
# ---------------------------------------------------------------------------


@dataclass
class _TwoPages(Provider):
    """Two pages; the worker is killed while the second page is being fetched."""

    second: list[Page] = field(default_factory=list)
    checkpoints: list[str | None] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    kill_on: str = ""

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.checkpoints.append(request.checkpoint)
        pages = self.pages if request.checkpoint is None else self.second
        objects = tuple(
            SourceObject(
                object_id=page.page_id,
                locator=f"https://wiki.example.com/{page.page_id}",
                kind=SourceObjectKind.FILE,
                version="1",
                media_type="text/plain",
                metadata={"classification": "unclassified", "title": page.title},
            )
            for page in pages
        )
        if request.checkpoint is None:
            return SyncSourcePage(objects=objects, next_checkpoint="c1", has_more=True)
        return SyncSourcePage(objects=objects, next_checkpoint=None)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        self.fetched.append(request.object_id)
        if request.object_id == self.kill_on:
            self.kill_on = ""
            supervisor = process_supervisor()
            await supervisor.kill_worker()

            async def down() -> bool:
                return supervisor.status().state != "up"

            assert await _until(down, seconds=10)
        page = next(p for p in [*self.pages, *self.second] if p.page_id == request.object_id)
        return SourceContent(
            object_id=page.page_id,
            version=request.version,
            media_type="text/plain",
            content=page.body.encode(),
        )


async def test_a_worker_killed_mid_sync_restarts_and_the_run_resumes_from_its_cursor(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    agent = await start_knowledge_agent(deployment, enable_modules)
    try:
        wiki = _TwoPages(
            "wiki",
            "confluence",
            "Team wiki",
            list(WIKI_PAGES[:2]),
            second=[Page("p-renewals", "Renewal Process", WIKI_PAGES[2].body)],
            kill_on="p-renewals",
        )
        proposal = await grant(agent, wiki)
        await approve(agent, proposal.approval_id)
        service = sync_service(agent)
        first_worker = await _worker_pid()
        await service.sync_now("wiki")

        async def paused() -> bool:
            rows = {row.connection_id: row for row in await service.list_sources()}
            row = rows.get("wiki")
            return row is not None and row.detail == "sync_worker_unavailable"

        assert await _until(paused), await service.list_sources()
        row = next(row for row in await service.list_sources() if row.connection_id == "wiki")
        assert row.state is not None and row.state.cursor == "c1", row.state
        # Search keeps working on what the first page committed, with no worker.
        found = await ask(agent, scripted_llm, "document_search", {"query": "billing portal"})
        assert "in August" in found, found

        async def restarted() -> bool:
            status = process_supervisor().status()
            return status.state == "up" and status.pid != first_worker

        assert await _until(restarted, seconds=30)
        await service.sync_now("wiki")

        async def complete() -> bool:
            return await _status_of(service, "wiki") == "complete"

        assert await _until(complete), await service.list_sources()
        assert wiki.checkpoints == [None, "c1", "c1"], wiki.checkpoints
        assert wiki.fetched.count("p-roadmap") == 1, "a committed page was fetched again"
        found = await ask(agent, scripted_llm, "document_search", {"query": "contract renewal"})
        assert "ninety days" in found, found
    finally:
        await agent.shutdown()


# ---------------------------------------------------------------------------
# The agent's loop stays responsive while the worker ingests a large account
# ---------------------------------------------------------------------------

_LARGE_PAGES = 240
_PAGE_BYTES = 24_000
#: The loop-lag budget the dashboard needs: chat, NATS and health all wait on it.
#: It is extra lag the ingest may add over this run's own idle loop.
_P99_BUDGET_MS = 50.0
#: One freeze this long is a blocking call on the loop, whatever the p99 says.
_MAX_BUDGET_MS = 500.0
#: How long the idle loop is sampled before the sync, as the run's own baseline.
_BASELINE_SECONDS = 2.0


def _large_account() -> list[Page]:
    words = "invoice ledger renewal roadmap onboarding contract billing portal".split()
    pages = []
    for index in range(_LARGE_PAGES):
        body = " ".join(words[(index + n) % len(words)] for n in range(_PAGE_BYTES // 8))
        pages.append(Page(f"p-large-{index:04d}", f"Large page {index}", body))
    return pages


async def test_the_agents_loop_stays_responsive_while_the_worker_ingests(
    deployment: Deployment, enable_modules: Any
) -> None:
    enable_modules(
        "memory",
        "connected_data",
        config={
            # Lexical only: the cost under test is extraction, chunking, FTS and
            # index writes, not a model download on a test box.
            "memory": {"brain": "arcmemory", "embed_backend": "none"},
            "connected_data": {"interval_seconds": 3600},
        },
    )
    toml = deployment.agent_dir / "arcagent.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8")
        + "\n[modules.connected_data.config.limits]\n"
        + f"max_pages = 10\nmax_bytes = {64 * 1024 * 1024}\nmax_seconds = 600\n"
        + "max_duty_fraction = 1.0\n",
        encoding="utf-8",
    )
    rows = install_modules(deployment)
    assert not [row for row in rows if "REFUSED" in row], rows
    agent = await start_agent(deployment)
    # arcui's own monitor, one sample per report, so every sample is kept: the
    # percentiles are taken over the whole ingest, not over half-second windows.
    lag_ms: list[float] = []
    monitor = asyncio.create_task(
        LoopLagMonitor(
            interval=0.01, report_every=0.0, report=lambda r: lag_ms.append(r.max_ms)
        ).run()
    )
    try:
        # The same process, on the same box, under the same load, before any sync.
        await asyncio.sleep(_BASELINE_SECONDS)
        baseline = summarize([ms / 1000 for ms in lag_ms])
        account = Provider("big", "confluence", "Large wiki", _large_account())
        proposal = await grant(agent, account)
        service = sync_service(agent)
        # The approval itself starts the first sync: measure from here to complete.
        lag_ms.clear()
        await approve(agent, proposal.approval_id)
        await service.sync_now("big")

        async def complete() -> bool:
            return await _status_of(service, "big") == "complete"

        assert await _until(complete, seconds=600), await service.list_sources()
        row = next(row for row in await service.list_sources() if row.connection_id == "big")
        assert row.documents_indexed == _LARGE_PAGES, row
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await agent.shutdown()
    assert len(lag_ms) >= 100, "the ingest finished before the loop could be measured"
    ingest = summarize([ms / 1000 for ms in lag_ms])
    shown = f"ingest {_shown(ingest)}; idle baseline {_shown(baseline)}"
    assert ingest.p99_ms < baseline.p99_ms + _P99_BUDGET_MS, shown
    assert ingest.max_ms < baseline.max_ms + _MAX_BUDGET_MS, shown


def _shown(report: LagReport) -> str:
    return (
        f"p50={report.p50_ms:.1f}ms p99={report.p99_ms:.1f}ms "
        f"max={report.max_ms:.1f}ms n={report.samples}"
    )
