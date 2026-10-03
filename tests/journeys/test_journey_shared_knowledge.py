"""Journey J1 F8 (P18-4): two agents granted one connection share one sync and one store.

Each agent used to crawl, store and embed the same account on its own, so the
fleet's provider calls, disk and embedding bill grew with the number of agents
granted a connection rather than with the data. Here two real ``ArcAgent``
instances share one arcstore and one fleet directory, the way the gateway runs a
fleet, and only the provider's pages and the LLM wire are scripted.

* one provider fetch per object, however many agents read it;
* revoking one agent hides the data from that agent only, at once, with no resync;
* a read for another agent, or of a store the agent was never granted, is refused;
* a run that dies mid-crawl is resumed by the other subscriber under the lease,
  from the committed cursor, without fetching anything twice.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from arcagent.extension.knowledge_subscriptions import knowledge_principal, store_key
from arcagent.extension.source import (
    FetchSourceObject,
    SourceContent,
    SourceObject,
    SourceObjectKind,
    SyncSource,
    SyncSourcePage,
)
from arcstore.backends.memory import FakeBackend
from arcstore.source_sync import ArcStoreSourceSyncStore
from arctrust.paths import connected_knowledge_dir

from .conftest import Deployment, ScriptedLLM, agent_toml
from .test_journey_knowledge import (
    WIKI_PAGES,
    Page,
    Provider,
    _until,
    approve,
    ask,
    connect,
    grant,
    start_knowledge_agent,
    sync_service,
    sync_to_completion,
)
from .test_journey_knowledge import _module_source as _module_source
from .test_journey_knowledge import arcstore as arcstore

_SYNC = {"interval_seconds": 3600}


@dataclass
class Account(Provider):
    """One account both agents are granted: every call either agent makes lands here."""

    fetched: dict[str, int] = field(default_factory=dict)
    checkpoints: list[str | None] = field(default_factory=list)

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.checkpoints.append(request.checkpoint)
        return await super().sync_source(request)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        self.fetched[request.object_id] = self.fetched.get(request.object_id, 0) + 1
        return await super().fetch_source(request)


def _write_second_agent(
    deployment: Deployment,
    *,
    limits: dict[str, float] | None,
    sync: dict[str, Any] | None = None,
) -> Path:
    """A second agent in the same fleet, written before ``arc install`` runs.

    Written again on a restart; its key is kept, so its DID is too.
    """
    from arccli.commands.agent.create import _mint_agent_identity

    agent_dir = deployment.team_root / "second_agent"
    (agent_dir / "workspace").mkdir(parents=True, exist_ok=True)
    body = agent_toml(
        agent_dir,
        name="second",
        modules=("memory", "connected_data"),
        module_config={
            "memory": {"__config__": {"brain": "arcmemory"}},
            "connected_data": {"__config__": {**_SYNC, **(sync or {})}},
        },
    )
    if limits:
        body += "\n[modules.connected_data.config.limits]\n" + "".join(
            f"{key} = {value}\n" for key, value in limits.items()
        )
    (agent_dir / "arcagent.toml").write_text(body, encoding="utf-8")
    (agent_dir / "arcllm.toml").write_text("[llm]\nmodel = 'scripted/model'\n", encoding="utf-8")
    _mint_agent_identity(agent_dir)
    return agent_dir


async def two_agents(
    deployment: Deployment,
    enable_modules: Any,
    *,
    limits: dict[str, float] | None = None,
    sync: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    import arcagent

    second_dir = _write_second_agent(deployment, limits=limits, sync=sync)
    first = await start_knowledge_agent(
        deployment, enable_modules, sync={**_SYNC, **(sync or {})}, limits=limits
    )
    config_path = second_dir / "arcagent.toml"
    second = arcagent.ArcAgent(arcagent.load_config(config_path), config_path=config_path)
    await second.startup()
    return first, second


def _store_root(connection_id: str) -> Path:
    return connected_knowledge_dir() / store_key(connection_id)


def _completed(agent: Any, connection_id: str) -> Any:
    async def check() -> bool:
        return (await _row(agent, connection_id)).status == "complete"

    return check


def _documents_under(root: Path) -> list[Path]:
    """The extracted wiki pages stored under ``root`` (not routing indexes or logs)."""
    return sorted(root.glob("memory/connected/**/p-*.md"))


async def _row(agent: Any, connection_id: str) -> Any:
    rows = await sync_service(agent).list_sources()
    return next(row for row in rows if row.connection_id == connection_id)


# ---------------------------------------------------------------------------
# One fetch per object
# ---------------------------------------------------------------------------


async def test_two_agents_granted_one_connection_fetch_each_object_once(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    first, second = await two_agents(deployment, enable_modules)
    try:
        account = Account("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
        await connect(first, account)
        await connect(second, account)

        assert account.fetched == {page.page_id: 1 for page in WIKI_PAGES}, account.fetched
        assert account.checkpoints.count(None) == 1, "the account was crawled from scratch twice"
        assert len(_documents_under(_store_root("wiki"))) == 3
        for agent in (first, second):
            found = await ask(agent, scripted_llm, "document_search", {"query": "billing portal"})
            assert "in August" in found, found
            row = await _row(agent, "wiki")
            assert row.status == "complete" and row.documents_indexed == 3, row
            assert row.state is not None and row.state.last_synced_at is not None
        # Neither agent keeps a copy of its own.
        for agent in (first, second):
            assert _documents_under(Path(agent._config.agent.workspace)) == []
    finally:
        await second.shutdown()
        await first.shutdown()


@dataclass
class GatedAccount(Account):
    """The first crawl waits at the provider until the test lets it go."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        if request.checkpoint is None:
            self.entered.set()
            await self.release.wait()
        return await super().sync_source(request)


async def test_two_agents_syncing_at_once_crawl_the_account_once(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """Both agents ask for a sync while the first crawl is provably in flight."""
    first, second = await two_agents(deployment, enable_modules)
    try:
        account = GatedAccount("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
        for agent in (first, second):
            await approve(agent, (await grant(agent, account)).approval_id)

        await sync_service(first).sync_now("wiki")
        await asyncio.wait_for(account.entered.wait(), timeout=30)
        # The first holds the lease, inside the provider. The second asks now.
        await sync_service(second).sync_now("wiki")

        async def second_adopted() -> bool:
            return (await _row(second, "wiki")).status in {"running", "syncing"} and not (
                sync_service(second)._tasks.get("wiki")
            )

        assert await _until(second_adopted), await _row(second, "wiki")
        account.release.set()
        for agent in (first, second):
            assert await _until(_completed(agent, "wiki")), await _row(agent, "wiki")
        assert account.checkpoints == [None], account.checkpoints
        assert account.fetched == {page.page_id: 1 for page in WIKI_PAGES}
        assert (await _row(second, "wiki")).documents_indexed == 3
    finally:
        account.release.set()
        await second.shutdown()
        await first.shutdown()


# ---------------------------------------------------------------------------
# Revoking one agent
# ---------------------------------------------------------------------------


async def test_revoking_one_agent_hides_the_store_from_that_agent_only_at_once(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    first, second = await two_agents(deployment, enable_modules)
    try:
        account = Account("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
        await connect(first, account)
        await connect(second, account)
        fetched = dict(account.fetched)

        revoked = await sync_service(second).revoke("wiki")
        assert revoked.status == "revoked", revoked

        gone = await ask(second, scripted_llm, "document_search", {"query": "billing portal"})
        kept = await ask(first, scripted_llm, "document_search", {"query": "billing portal"})
        assert "in August" not in gone, gone
        assert "in August" in kept, kept
        assert account.fetched == fetched, "a revoke made the other agent resync"
        assert _store_root("wiki").is_dir(), "the store went while an agent still reads it"

        # The last reader leaves: the store has no one left to serve and is purged.
        assert (await sync_service(first).revoke("wiki")).status == "revoked"
        assert not _store_root("wiki").exists()
        none = await ask(first, scripted_llm, "document_search", {"query": "billing portal"})
        assert "in August" not in none, none
    finally:
        await second.shutdown()
        await first.shutdown()


# ---------------------------------------------------------------------------
# Cross-agent reads (adversarial battery)
# ---------------------------------------------------------------------------


async def test_an_agent_cannot_read_a_store_it_was_not_granted_or_approved(
    deployment: Deployment,
    enable_modules: Any,
    scripted_llm: ScriptedLLM,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ASI03: the store is the connection's, but every read is the reading agent's."""
    first, second = await two_agents(deployment, enable_modules)
    try:
        account = Account("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
        await connect(first, account)
        pool = (await _row(first, "wiki")).source_id
        second_did = second._identity.did

        # Never granted: naming the shared pool's id outright finds nothing.
        named = await ask(
            second, scripted_llm, "document_search", {"query": "billing portal", "source": pool}
        )
        assert "in August" not in named, named
        assert (
            await sync_service(second).shared_document_search(
                "billing portal", caller_did=second_did, source_ids=[pool]
            )
            == []
        )

        # Granted but its own mapping not yet approved: still nothing.
        await grant(second, account)
        await sync_service(second).sync_now("wiki")
        await asyncio.sleep(0.3)
        pending = await ask(second, scripted_llm, "document_search", {"query": "billing portal"})
        assert "in August" not in pending, pending

        # Asking the first agent's service on the second agent's behalf is refused.
        with caplog.at_level(logging.INFO, logger="arcagent.audit"):
            borrowed = await sync_service(first).shared_document_search(
                "billing portal", caller_did=second_did
            )
        assert borrowed == []
        assert "connected_data.knowledge.read_refused" in caplog.text
    finally:
        await second.shutdown()
        await first.shutdown()


# ---------------------------------------------------------------------------
# A run that dies mid-crawl
# ---------------------------------------------------------------------------

_TWO_PAGE = [
    Page("p-one", "Page one", "The billing portal ships in August."),
    Page("p-two", "Page two", "New hires get a laptop in week one."),
    Page("p-three", "Page three", "Renewals are reviewed ninety days ahead."),
]


@dataclass
class WedgingAccount(Account):
    """Two pages. The first caller to ask for page two never hears back: a crash."""

    wedged: asyncio.Event = field(default_factory=asyncio.Event)
    never: asyncio.Event = field(default_factory=asyncio.Event)

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.checkpoints.append(request.checkpoint)
        self.calls["sync_source"] = self.calls.get("sync_source", 0) + 1
        first_page, second_page = self.pages[:2], self.pages[2:]
        if request.checkpoint is None:
            return self._page(first_page, next_checkpoint="c1", has_more=True)
        if request.checkpoint == "c1" and not self.wedged.is_set():
            self.wedged.set()
            await self.never.wait()
        if request.checkpoint == "c1":
            return self._page(second_page, next_checkpoint="c2", has_more=False)
        return SyncSourcePage(next_checkpoint=request.checkpoint)

    def _page(self, pages: list[Page], *, next_checkpoint: str, has_more: bool) -> SyncSourcePage:
        return SyncSourcePage(
            objects=_objects(self.kind, pages), next_checkpoint=next_checkpoint, has_more=has_more
        )


def _objects(kind: str, pages: list[Page]) -> tuple[SourceObject, ...]:
    return tuple(
        SourceObject(
            object_id=page.page_id,
            locator=f"https://{kind}.example.com/pages/{page.page_id}",
            kind=SourceObjectKind.FILE,
            version="1",
            media_type="text/plain",
            metadata={"classification": "unclassified", "title": page.title},
        )
        for page in pages
    )


async def test_a_crawl_that_dies_mid_run_is_resumed_by_the_other_agent_under_the_lease(
    deployment: Deployment,
    enable_modules: Any,
    scripted_llm: ScriptedLLM,
    arcstore: FakeBackend,  # noqa: F811 — the module's shared-arcstore fixture
) -> None:
    limits = {"max_seconds": 1.0, "retries": 0, "max_duty_fraction": 1.0}
    first, second = await two_agents(deployment, enable_modules, limits=limits)
    store = ArcStoreSourceSyncStore(arcstore)
    principal = knowledge_principal("wiki")
    try:
        account = WedgingAccount("wiki", "confluence", "Team wiki", list(_TWO_PAGE))
        for agent in (first, second):
            await approve(agent, (await grant(agent, account)).approval_id)

        await sync_service(first).sync_now("wiki")
        await asyncio.wait_for(account.wedged.wait(), timeout=30)
        # Page one is committed; the run holding the lease is now dead in the water.
        assert (await store.get_state(principal, "wiki")).cursor == "c1"

        async def lease_expired() -> bool:
            rows = await store.list_for_connection("wiki")
            now = datetime.now(UTC)
            return not any(row.is_live(now) for row in rows if row.agent_did == principal)

        assert await _until(lease_expired, seconds=10)
        await sync_service(second).sync_now("wiki")

        async def resumed() -> bool:
            return (await store.get_state(principal, "wiki")).status.value == "complete"

        assert await _until(resumed, seconds=30), await store.get_state(principal, "wiki")
        assert account.checkpoints == [None, "c1", "c1"], account.checkpoints
        assert account.fetched == {page.page_id: 1 for page in _TWO_PAGE}, account.fetched
        found = await ask(second, scripted_llm, "document_search", {"query": "renewals reviewed"})
        assert "ninety days ahead" in found, found
    finally:
        await second.shutdown()
        await first.shutdown()
    # The dead run, cancelled at shutdown, is fenced: it cannot undo the resumed one.
    final = await store.get_state(principal, "wiki")
    assert final.status.value == "complete" and final.cursor == "c2"


# ---------------------------------------------------------------------------
# Migrating stores the agents already hold
# ---------------------------------------------------------------------------


async def _upgraded(
    deployment: Deployment, enable_modules: Any, *, shared_stores: bool = True
) -> tuple[Any, Any]:
    """Restart both agents, keeping each one's identity; ``shared_stores`` is the opt-in."""
    import arcagent

    wanted = "true" if shared_stores else "false"
    for agent_dir in (deployment.agent_dir, deployment.team_root / "second_agent"):
        toml = agent_dir / "arcagent.toml"
        text = toml.read_text(encoding="utf-8")
        flipped = text.replace("shared_stores = false", f"shared_stores = {wanted}")
        toml.write_text(flipped, "utf-8")
    first = await start_knowledge_agent(deployment, enable_modules, installed=True)
    config_path = deployment.team_root / "second_agent" / "arcagent.toml"
    second = arcagent.ArcAgent(arcagent.load_config(config_path), config_path=config_path)
    await second.startup()
    return first, second


async def _registered(agent: Any, account: Provider) -> None:
    """After a restart the connectors module registers the grant again; so does the test."""
    await agent._runtime_deps.source_catalog.register(account.connection_id, account)

    async def described() -> bool:
        return (await _row(agent, account.connection_id)).description is not None

    assert await _until(described)


async def _held_by_two_agents(
    deployment: Deployment, enable_modules: Any, account: Account
) -> None:
    """Both agents sync the wiki into their own stores, with shared stores off."""
    first, second = await two_agents(deployment, enable_modules, sync={"shared_stores": False})
    try:
        await connect(first, account)
        await connect(second, account)
    finally:
        await second.shutdown()
        await first.shutdown()
    assert account.fetched == {page.page_id: 2 for page in WIKI_PAGES}


async def _migrated(agents: tuple[Any, ...]) -> bool:
    """Whether every agent's own copy is gone and the shared store holds the wiki."""
    own = [_documents_under(Path(agent._config.agent.workspace)) for agent in agents]
    return not any(own) and len(_documents_under(_store_root("wiki"))) == 3


async def test_stores_the_agents_already_hold_move_into_one_on_their_own(
    deployment: Deployment,
    enable_modules: Any,
    scripted_llm: ScriptedLLM,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DGX upgrade: two agents each synced the wiki; restart, and there is one copy.

    Nobody runs a command. Startup is not held up by the move; the documents are
    re-keyed into the shared store by object id and version, the store continues
    from the agents' own cursor, no provider is asked for anything, and a second
    start finds nothing left to do.
    """
    from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter

    account = Account("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
    await _held_by_two_agents(deployment, enable_modules, account)

    release = asyncio.Event()
    adopt = ArcMemoryIngestAdapter.adopt_documents

    async def held(self: Any, *args: Any, dry_run: bool, **kwargs: Any) -> Any:
        if not dry_run:
            await release.wait()
        return await adopt(self, *args, dry_run=dry_run, **kwargs)

    monkeypatch.setattr(ArcMemoryIngestAdapter, "adopt_documents", held)
    # The agent that finds the other one mid-move tries again soon, not in an hour.
    monkeypatch.setattr("arcagent.modules.connected_data.service._MIGRATION_RETRY_SECONDS", 0.5)
    with caplog.at_level(logging.INFO, logger="arcagent.audit"):
        first, second = await _upgraded(deployment, enable_modules)  # startup returned
        try:
            for agent in (first, second):
                await _registered(agent, account)
                # Until it is moved an agent keeps reading its own copy.
                held_copy = await ask(
                    agent, scripted_llm, "document_search", {"query": "billing portal"}
                )
                assert "in August" in held_copy, held_copy
            assert _documents_under(_store_root("wiki")) == [], "moved before it was released"
            fetched = dict(account.fetched)

            # A preview changes nothing.
            preview = await sync_service(first).preview_migration()
            assert [(row.status, row.adopted) for row in preview] == [("would_migrate", 3)]
            assert _documents_under(_store_root("wiki")) == []

            release.set()
            agents = (first, second)

            async def done() -> bool:
                return await _migrated(agents)

            assert await _until(done), "the agents' own copies were never moved"
            assert account.fetched == fetched, "the migration fetched from the provider"
            for agent in agents:
                found = await ask(
                    agent, scripted_llm, "document_search", {"query": "billing portal"}
                )
                assert "in August" in found, found
                pointers = re.findall(r'<memory-result source="([^"]*)"', found)
                assert len(pointers) == len(set(pointers)), (
                    f"a document was read twice: {pointers}"
                )
            moved = caplog.text.count("connected_data.knowledge.migration")
            assert moved >= 2  # one per agent (plus the preview)
        finally:
            await second.shutdown()
            await first.shutdown()

        # A second start has nothing to move and says nothing.
        before = caplog.text.count("connected_data.knowledge.migration")
        first, second = await _upgraded(deployment, enable_modules)
        try:
            for agent in (first, second):
                await _registered(agent, account)
                await sync_to_completion(agent, "wiki")
            assert caplog.text.count("connected_data.knowledge.migration") == before
            assert account.fetched == fetched
            assert await _migrated((first, second))
        finally:
            await second.shutdown()
            await first.shutdown()


async def test_a_migration_that_cannot_prove_every_document_landed_keeps_the_own_copy(
    deployment: Deployment,
    enable_modules: Any,
    scripted_llm: ScriptedLLM,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store that reports success but holds fewer documents never costs the agent its copy."""
    from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter

    account = Account("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
    await _held_by_two_agents(deployment, enable_modules, account)

    async def drops_everything(self: Any, *args: Any, dry_run: bool, **kwargs: Any) -> Any:
        del self, args, dry_run, kwargs
        return {"documents": 3, "adopted": 3, "deduplicated": 0, "skipped": 0}

    monkeypatch.setattr(ArcMemoryIngestAdapter, "adopt_documents", drops_everything)
    with caplog.at_level(logging.INFO, logger="arcagent"):
        first, second = await _upgraded(deployment, enable_modules)
        try:
            await _registered(first, account)
            await _registered(second, account)

            async def warned() -> bool:
                return caplog.text.count("read_back_mismatch") >= 1

            assert await _until(warned), caplog.text
            for agent in (first, second):
                assert len(_documents_under(Path(agent._config.agent.workspace))) == 3
                found = await ask(
                    agent, scripted_llm, "document_search", {"query": "billing portal"}
                )
                assert "in August" in found, found
            assert "connected_data.knowledge.migration" in caplog.text
        finally:
            await second.shutdown()
            await first.shutdown()


async def test_an_agent_that_opted_out_of_shared_stores_is_never_moved(
    deployment: Deployment,
    enable_modules: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = Account("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
    await _held_by_two_agents(deployment, enable_modules, account)

    with caplog.at_level(logging.INFO, logger="arcagent.audit"):
        first, second = await _upgraded(deployment, enable_modules, shared_stores=False)
        try:
            for agent in (first, second):
                await _registered(agent, account)
                await sync_to_completion(agent, "wiki")
            assert _documents_under(_store_root("wiki")) == []
            for agent in (first, second):
                assert len(_documents_under(Path(agent._config.agent.workspace))) == 3
            assert "connected_data.knowledge.migration" not in caplog.text
        finally:
            await second.shutdown()
            await first.shutdown()
