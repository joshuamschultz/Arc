"""Journey J1: connect business tools and get great results, driven through the agent.

A person connects Confluence and mail, approves what the agent may learn, and then
asks a question. Every gate here goes through the real path: a started ``ArcAgent``
with its real memory and connected-data modules, the real sync service, the real
arcstore, the real document index and (when cached) the real local embedder. Only
the LLM wire and the provider's pages are scripted. The model's own tool call is
what reaches ``document_search``; nothing calls the brain with an internal id.

Why it exists: on the live fleet ``document_search`` answered "nothing found" on 21
of 21 calls while a 29-test suite stayed green, because the tests passed the hashed
internal source id the model never sees.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcagent.connected_data import KnowledgeHome
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceObject,
    SourceObjectKind,
    SyncSource,
    SyncSourcePage,
)
from arcagent.tools.approval_store import open_approval_store
from arcstore.backends.memory import FakeBackend

from .conftest import Deployment, ScriptedLLM, ScriptedTurn
from .test_journey_modules import _SOURCE_CATALOG, install_modules, start_agent

_UPDATED = "2026-09-30T10:00:00Z"


@pytest.fixture(autouse=True)
def _module_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(_SOURCE_CATALOG))


@pytest.fixture(autouse=True)
def arcstore(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    """One arcstore for the whole test, so a restarted agent finds what the first left.

    Production arcstore is PostgreSQL, which a unit-test box does not run; the
    contract fake implements the same backend seam (approvals, mutable rows, sync
    state). It outlives each ``ArcAgent``, as the database outlives a restart.
    """
    import arcagent

    backend = FakeBackend()

    def opener(self: Any) -> Callable[[], Awaitable[FakeBackend]]:
        async def open_backend() -> FakeBackend:
            return backend

        return open_backend

    monkeypatch.setattr(arcagent.ArcAgent, "_make_arcstore_opener", opener)
    return backend


# ---------------------------------------------------------------------------
# The provider: pages the account would answer with
# ---------------------------------------------------------------------------


@dataclass
class Page:
    page_id: str
    title: str
    body: str


@dataclass
class Provider:
    """A document source with a fixed set of pages; counts every call it is asked."""

    connection_id: str
    kind: str
    name: str
    pages: list[Page]
    calls: dict[str, int] = field(default_factory=dict)

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        self._count("inspect_source")
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind=self.kind,
            account_id=f"{self.kind}-account",
            data_shape=SourceDataShape.DOCUMENT,
            display_name=self.name,
            root_locator="/",
        )

    async def list_source_resources(self, request: ListSourceResources) -> tuple[Any, ...]:
        self._count("list_source_resources")
        return ()

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self._count("sync_source")
        if request.checkpoint is not None:
            return SyncSourcePage(next_checkpoint=request.checkpoint)
        objects = tuple(
            SourceObject(
                object_id=page.page_id,
                locator=f"https://{self.kind}.example.com/pages/{page.page_id}",
                kind=SourceObjectKind.FILE,
                version="1",
                modified_at=_UPDATED,
                media_type="text/plain",
                metadata={
                    "classification": "unclassified",
                    "title": page.title,
                    "url": f"https://{self.kind}.example.com/pages/{page.page_id}",
                },
            )
            for page in self.pages
        )
        return SyncSourcePage(objects=objects, next_checkpoint="c1")

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        self._count("fetch_source")
        page = next(page for page in self.pages if page.page_id == request.object_id)
        return SourceContent(
            object_id=page.page_id,
            version=request.version,
            media_type="text/plain",
            content=page.body.encode(),
        )

    async def close_source(self) -> None:
        return None


WIKI_PAGES = [
    Page(
        "p-roadmap",
        "Q3 Roadmap",
        "The Q3 roadmap commits to shipping the billing portal in August.",
    ),
    Page(
        "p-onboarding",
        "New Hire Onboarding",
        "New hires receive a laptop and a buddy in week one.",
    ),
]
WIKI_PAGES.append(
    Page(
        "p-renewals",
        "Renewal Process",
        "Account managers review every contract renewal ninety days ahead.",
    )
)
MAIL_PAGES = [
    Page(
        "m-renewal",
        "Renewal notice from Acme",
        "Acme confirmed the contract renewal for twelve months.",
    ),
]


# ---------------------------------------------------------------------------
# The deployment: a real agent with memory + connected data, and approvals
# ---------------------------------------------------------------------------


async def start_knowledge_agent(
    deployment: Deployment,
    enable_modules: Any,
    *,
    installed: bool = False,
    sync: dict[str, Any] | None = None,
    limits: dict[str, float] | None = None,
) -> Any:
    """Start the agent; ``installed`` restarts one whose modules are already in place.

    ``sync`` overrides ``[modules.connected_data.config]`` keys and ``limits`` adds the
    ``limits`` sub-table; both are plain operator TOML, nothing is patched.
    """
    if not installed:
        enable_modules(
            "memory",
            "connected_data",
            config={
                "memory": {"brain": "arcmemory"},
                "connected_data": {"interval_seconds": 3600, **(sync or {})},
            },
        )
        if limits:
            toml = deployment.agent_dir / "arcagent.toml"
            body = "".join(f"{key} = {value}\n" for key, value in limits.items())
            toml.write_text(
                toml.read_text(encoding="utf-8")
                + f"\n[modules.connected_data.config.limits]\n{body}",
                encoding="utf-8",
            )
        rows = install_modules(deployment)
        assert not [row for row in rows if "REFUSED" in row], f"install refused: {rows}"
    return await start_agent(deployment)


def sync_service(agent: Any) -> Any:
    for binding in agent._runtime_bindings:
        if binding.module_name == "connected_data":
            assert binding.state.service is not None, "connected-data service never started"
            return binding.state.service
    raise AssertionError("connected_data module was not configured")


async def _until(check: Callable[[], Awaitable[bool]], *, seconds: float = 60.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        if await check():
            return True
        await asyncio.sleep(0.05)
    return False


async def _status_of(service: Any, connection_id: str) -> str:
    for row in await service.list_sources():
        if row.connection_id == connection_id:
            return str(row.status)
    return "absent"


async def grant(agent: Any, provider: Provider) -> Any:
    """The operator grants a connection and stages what the agent may learn from it."""
    service = sync_service(agent)
    await agent._runtime_deps.source_catalog.register(provider.connection_id, provider)

    async def described() -> bool:
        return any(
            row.connection_id == provider.connection_id and row.description is not None
            for row in await service.list_sources()
        )

    assert await _until(described), "the source was never described"
    proposal = await service.stage_mapping(provider.connection_id, homes=(KnowledgeHome.DOCUMENT,))
    assert proposal is not None, "the mapping could not be staged"
    return proposal


async def approve(agent: Any, approval_id: str) -> None:
    """The operator approves the staged mapping in the shared approvals directory."""
    approvals, _ = await open_approval_store(opener=agent._arcstore_opener)
    await approvals.resolve(
        approval_id,
        status="approved",
        actor_did="did:arc:operator",
        resolved_by="did:arc:operator",
    )


async def sync_to_completion(agent: Any, connection_id: str) -> None:
    service = sync_service(agent)
    await service.sync_now(connection_id)

    async def synced() -> bool:
        return await _status_of(service, connection_id) == "complete"

    assert await _until(synced), f"{connection_id} never completed: {await service.list_sources()}"


async def connect(agent: Any, provider: Provider) -> None:
    """Grant, approve as the operator, and wait for the first sync to land."""
    proposal = await grant(agent, provider)
    await approve(agent, proposal.approval_id)
    await sync_to_completion(agent, provider.connection_id)


def tool_output(llm: ScriptedLLM) -> str:
    """The text the model was handed back for its most recent tool call."""
    results: list[str] = []
    for message in llm.calls[-1]:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        results.extend(
            str(block.content) for block in content if getattr(block, "type", "") == "tool_result"
        )
    assert results, "the model never received a tool result"
    return results[-1]


_sessions = iter(range(10_000))


async def ask(agent: Any, llm: ScriptedLLM, tool: str, args: dict[str, Any]) -> str:
    """One real turn in which the model calls ``tool``; returns what the tool answered."""
    llm.replies.extend([ScriptedTurn(tool=tool, args=args), "ok"])
    session = await agent.session(f"knowledge-{next(_sessions)}")
    async for _ in agent.run("please look that up", session=session):
        pass
    return tool_output(llm)


async def say(agent: Any, text: str) -> None:
    """One plain turn: the user says something, the scripted model just answers."""
    session = await agent.session(f"knowledge-{next(_sessions)}")
    async for _ in agent.run(text, session=session):
        pass


def sources_in(output: str) -> set[str]:
    """The ``Source:`` kinds a rendered hit list cites."""
    return {
        part.split("Source:")[1].split("|")[0].strip()
        for part in output.split("\n")
        if "Source:" in part
    }


def pointers_in(output: str) -> list[str]:
    return re.findall(r'<memory-result source="([^"]*)"', output)


def titles_in(output: str) -> list[str]:
    return [
        part.split("Title:")[1].split("|")[0].strip()
        for part in output.split("\n")
        if part.startswith("Title:")
    ]


async def two_sources(agent: Any) -> tuple[Provider, Provider]:
    wiki = Provider("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
    mail = Provider("mail", "gmail", "Josh mail", list(MAIL_PAGES))
    await connect(agent, wiki)
    await connect(agent, mail)
    return wiki, mail


# ---------------------------------------------------------------------------
# G1, G2: the model finds what was synced, by asking the way a model asks
# ---------------------------------------------------------------------------


async def test_g1_document_search_with_no_source_finds_hits_across_approved_sources(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """The model omits ``source`` (as the tool description tells it to) and gets hits.

    On the live fleet this returned "No document results found" every time, because
    an omitted source meant "search nothing". One query must reach both sources.
    """
    agent = await start_knowledge_agent(deployment, enable_modules)
    try:
        await two_sources(agent)

        out = await ask(agent, scripted_llm, "document_search", {"query": "contract renewal"})

        assert "No document results" not in out
        assert sources_in(out) == {"confluence", "gmail"}, out
        assert "Renewal notice from Acme" in out
        assert "ninety days ahead" in out
    finally:
        await agent.shutdown()


async def test_g2_a_display_name_or_kind_resolves_to_the_source(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """The model only ever sees names; each of them must narrow to the right source."""
    agent = await start_knowledge_agent(deployment, enable_modules)
    try:
        await two_sources(agent)

        for name in ("Team wiki", "team wiki", "confluence", "wiki"):
            out = await ask(
                agent,
                scripted_llm,
                "document_search",
                {"query": "contract renewal", "source": name},
            )
            assert sources_in(out) == {"confluence"}, f"{name!r} -> {out}"

        out = await ask(
            agent, scripted_llm, "document_search", {"query": "contract renewal", "source": "mail"}
        )
        assert sources_in(out) == {"gmail"}, out

        out = await ask(
            agent,
            scripted_llm,
            "document_search",
            {"query": "contract renewal", "source": "sharepoint"},
        )
        assert "No connected source is named 'sharepoint'" in out
        assert "Team wiki" in out and "Josh mail" in out
    finally:
        await agent.shutdown()


# ---------------------------------------------------------------------------
# G3, G4: the catalog the model reads survives a restart and never calls a provider
# ---------------------------------------------------------------------------


async def test_g3_the_catalog_survives_a_restart(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """After a restart ``connected_sources`` lists the source and the prompt names it.

    The staged mapping comes back from the durable store as plain strings; both the
    tool and the prompt hook once crashed on it (``'str' has no attribute 'value'``),
    so after every restart the agent could not say what was connected.
    """
    agent = await start_knowledge_agent(deployment, enable_modules)
    try:
        await connect(agent, Provider("wiki", "confluence", "Team wiki", list(WIKI_PAGES)))
    finally:
        await agent.shutdown()

    restarted = await start_knowledge_agent(deployment, enable_modules, installed=True)
    try:
        await restarted._runtime_deps.source_catalog.register(
            "wiki", Provider("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
        )
        service = sync_service(restarted)

        async def described() -> bool:
            return any(row.description is not None for row in await service.list_sources())

        assert await _until(described)

        out = await ask(restarted, scripted_llm, "connected_sources", {})
        assert "Team wiki: confluence" in out and "homes=document" in out, out
        assert "Error" not in out

        # The same restart: the prompt assembled for the next turn carries the section.
        await say(restarted, "what do we have connected?")
        assert "Team wiki (confluence)" in scripted_llm.last_prompt_text
    finally:
        await restarted.shutdown()


async def test_g4_prompt_assembly_makes_no_provider_calls(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """Ten assembled prompts cost the provider nothing, and each one is fast.

    The catalog is read on every turn of every agent granted a connection; a vendor
    CLI that hangs must not be able to stall them. Counted at the provider itself.
    """
    from arcagent.modules.connected_data.capabilities import inject_connections_catalog

    agent = await start_knowledge_agent(deployment, enable_modules)
    try:
        wiki = Provider("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
        await connect(agent, wiki)
        before = dict(wiki.calls)

        binding = next(b for b in agent._runtime_bindings if b.module_name == "connected_data")
        binding.activate()
        slowest = 0.0
        for _ in range(10):
            ctx = SimpleNamespace(data={"sections": {}})
            started = time.perf_counter()
            await inject_connections_catalog(ctx)
            slowest = max(slowest, time.perf_counter() - started)
            assert "Team wiki (confluence)" in ctx.data["sections"]["connections"]
        for _ in range(10):
            await say(agent, "hello")

        assert wiki.calls == before, (
            f"the provider was called while assembling prompts: {wiki.calls}"
        )
        assert slowest < 0.05, f"assembling the catalog took {slowest * 1000:.1f} ms"
    finally:
        await agent.shutdown()


# ---------------------------------------------------------------------------
# G6: a stalled sync is a durable failure the operator can see
# ---------------------------------------------------------------------------


class _HangingProvider(Provider):
    hang: bool = False

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        if self.hang:
            await asyncio.sleep(3600)
        return await super().sync_source(request)


async def test_g6_a_stalled_sync_is_durable_and_visible(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM, arcstore: FakeBackend
) -> None:
    """A provider that never answers leaves ``failed/sync_stalled`` and the last good time.

    Jira sat ``running`` for nine days because a stall only touched in-memory state.
    The durable row is read back through a fresh store object, as a restarted process
    or the operator's card would.
    """
    from arcstore.source_sync import ArcStoreSourceSyncStore

    agent = await start_knowledge_agent(
        deployment,
        enable_modules,
        sync={"stall_grace_seconds": 0.2, "restart_backoff_seconds": 600},
        limits={"max_seconds": 0.3, "retries": 0, "max_duty_fraction": 1.0},
    )
    try:
        provider = _HangingProvider("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
        await connect(agent, provider)
        did = _sync_owner(agent)
        good = (
            await ArcStoreSourceSyncStore(arcstore).get_state(did, _source_id(agent))
        ).last_synced_at
        assert good is not None

        provider.hang = True
        await sync_service(agent).sync_now("wiki")

        async def failed() -> bool:
            state = await ArcStoreSourceSyncStore(arcstore).get_state(did, _source_id(agent))
            return state.status.value == "failed"

        assert await _until(failed, seconds=30), "a stall left the durable row 'running'"
        durable = await ArcStoreSourceSyncStore(arcstore).get_state(did, _source_id(agent))
        listed = next(row for row in await sync_service(agent).list_sources())

        # Unwell is not gone: the agent still sees the source and its old pages.
        sources = await ask(agent, scripted_llm, "connected_sources", {})
        assert "Team wiki: confluence; status=failed" in sources, sources
        found = await ask(
            agent,
            scripted_llm,
            "document_search",
            {"query": "billing portal", "source": "Team wiki"},
        )
        assert "billing portal" in found, found
    finally:
        await agent.shutdown()

    assert durable.error_code == "sync_stalled"
    assert durable.last_synced_at == good, "the failure erased the last good sync"
    assert listed.status == "failed" and listed.detail == "sync_stalled"
    assert listed.state is not None and listed.state.last_synced_at == good


def _source_id(agent: Any) -> str:
    """The key the sync store files the wiki under: the coordinator keys by connection id."""
    del agent
    return "wiki"


def _sync_owner(agent: Any) -> str:
    """Whose row the wiki's sync advances: the connection's shared store, not one agent.

    Every agent granted the wiki reads one store synced once (P18-4), so the durable
    row is the connection's, and the card shows it on each agent's row.
    """
    from arcagent.modules.connected_data.shared import knowledge_principal

    del agent
    return knowledge_principal("wiki")


# ---------------------------------------------------------------------------
# G8: revoking a source removes what it taught the agent, and a re-grant starts over
# ---------------------------------------------------------------------------


async def test_g8_revoking_a_source_removes_its_knowledge_and_a_regrant_needs_reapproval(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """The retrieval boundary: gone from search at once, the other source untouched.

    ``revoke`` is the call connector reconcile makes when a grant is withdrawn. A
    re-granted connection is a new decision: nothing is learned from it until the
    operator approves its mapping again.
    """
    agent = await start_knowledge_agent(deployment, enable_modules)
    try:
        await two_sources(agent)
        before = await ask(agent, scripted_llm, "document_search", {"query": "billing portal"})
        assert "in August" in before

        revoked = await sync_service(agent).revoke("wiki")
        assert revoked.status == "revoked", revoked

        gone = await ask(agent, scripted_llm, "document_search", {"query": "billing portal"})
        assert "in August" not in gone and "confluence" not in sources_in(gone), gone
        named = await ask(
            agent,
            scripted_llm,
            "document_search",
            {"query": "billing portal", "source": "Team wiki"},
        )
        assert "No connected source is named" in named
        kept = await ask(agent, scripted_llm, "document_search", {"query": "contract renewal"})
        assert sources_in(kept) == {"gmail"}, "revoking the wiki disturbed the mail source"

        # The operator grants the wiki again: nothing is learned until it is approved again.
        again = Provider("wiki", "confluence", "Team wiki", list(WIKI_PAGES))
        proposal = await grant(agent, again)
        assert proposal.approval_status == "pending", "a re-grant inherited the old approval"
        await sync_service(agent).sync_now("wiki")
        await asyncio.sleep(0.5)
        assert again.calls.get("fetch_source", 0) == 0, (
            "the wiki was read before it was re-approved"
        )
        still = await ask(agent, scripted_llm, "document_search", {"query": "billing portal"})
        assert "in August" not in still and "confluence" not in sources_in(still), still

        await approve(agent, proposal.approval_id)
        await sync_to_completion(agent, "wiki")
        back = await ask(agent, scripted_llm, "document_search", {"query": "billing portal"})
        assert "in August" in back
    finally:
        await agent.shutdown()


# ---------------------------------------------------------------------------
# G10: a hit carries what a person needs to check it, and keeps it across a restart
# ---------------------------------------------------------------------------

_ROADMAP_CITATION = (
    "Title: Q3 Roadmap | Source: confluence | "
    "Link: https://confluence.example.com/pages/p-roadmap | Updated: 2026-09-30T10:00:00Z"
)


async def test_g10_hits_are_citable_and_survive_a_restart(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """Title, source, link and updated time reach the model, and the prompt asks it to cite."""
    agent = await start_knowledge_agent(deployment, enable_modules)
    try:
        await connect(agent, Provider("wiki", "confluence", "Team wiki", list(WIKI_PAGES)))
        first = await ask(agent, scripted_llm, "document_search", {"query": "billing portal"})
        assert _ROADMAP_CITATION in first, first

        await say(agent, "what is connected?")
        assert "cite it" in scripted_llm.last_prompt_text
    finally:
        await agent.shutdown()

    restarted = await start_knowledge_agent(deployment, enable_modules, installed=True)
    try:
        after = await ask(restarted, scripted_llm, "document_search", {"query": "billing portal"})
        assert _ROADMAP_CITATION in after, after
    finally:
        await restarted.shutdown()


# ---------------------------------------------------------------------------
# G12: the right page is in the top three, through the tool, with the real embedder
# ---------------------------------------------------------------------------

_P21_RELEVANCE = (
    Path(__file__).resolve().parents[2]
    / "packages/arcmemory/tests/unit/test_doc_search_relevance.py"
)


def _relevance_set() -> tuple[dict[str, str], list[tuple[str, str]]]:
    """The 50 seeded pages and 10 paraphrased questions the arcmemory gate already uses."""
    spec = importlib.util.spec_from_file_location("p21_relevance_set", _P21_RELEVANCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module._PAGES), list(module._QUERIES)


async def _require_local_embedder() -> None:
    """Skip, with the reason, when all-MiniLM-L6-v2 is not in the offline model cache."""
    embeddings = pytest.importorskip("arcllm.embeddings")
    pytest.importorskip("sentence_transformers")
    try:
        await embeddings.LocalEmbedder(embeddings.DEFAULT_EMBED_MODEL).embed(["probe"])
    except embeddings.ArcLLMEmbeddingUnavailableError as error:
        pytest.skip(f"all-MiniLM-L6-v2 weights are not in the offline model cache: {error}")


async def test_g12_the_right_page_is_in_the_top_three_for_eight_of_ten_queries(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """Fifty wiki pages, ten paraphrased questions, asked the way the model asks them.

    Each question shares few words with its page, so only the embedder can find it.
    The model passes no source. The page must be among the first three hits, and no
    pointer may repeat.
    """
    await _require_local_embedder()
    pages, queries = _relevance_set()
    agent = await start_knowledge_agent(deployment, enable_modules)
    try:
        wiki = Provider(
            "wiki",
            "confluence",
            "Team wiki",
            [Page(key, key, body) for key, body in pages.items()],
        )
        await connect(agent, wiki)

        found = 0
        misses: list[str] = []
        for question, page_id in queries:
            out = await ask(agent, scripted_llm, "document_search", {"query": question})
            pointers = pointers_in(out)
            assert len(pointers) == len(set(pointers)), f"duplicate pointers for {question!r}"
            if page_id in titles_in(out)[:3]:
                found += 1
            else:
                misses.append(f"{question!r} wanted {page_id}, got {titles_in(out)[:3]}")
        assert found >= 8, f"only {found}/10 queries put the right page in the top 3: {misses}"
    finally:
        await agent.shutdown()


# ---------------------------------------------------------------------------
# Item 28: connected documents reach the agent at task start, one hit per document
# ---------------------------------------------------------------------------


def _warranty_pages() -> list[Page]:
    """One very long page (many chunks, all on topic) beside three short ones."""
    steps = [
        f"Step {n}: for a gizmo warranty claim the support lead records the serial number, "
        f"checks purchase date {n}, and confirms the failure mode with the customer. "
        + ("The claim file keeps every photo, every message and every approval. " * 12)
        for n in range(1, 11)
    ]
    return [
        Page("p-warranty-manual", "Gizmo Warranty Manual", "\n\n".join(steps)),
        Page("p-warranty-faq", "Gizmo Warranty FAQ", "A gizmo warranty claim takes five days."),
        Page("p-warranty-terms", "Gizmo Warranty Terms", "The gizmo warranty covers two years."),
        Page(
            "p-warranty-email",
            "Gizmo Warranty Email Template",
            "Dear customer, your gizmo warranty claim is open.",
        ),
    ]


async def test_item_28_documents_reach_the_agent_at_task_start_one_hit_per_document(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """The model is never asked to search: the first prompt already holds the pages.

    A long page must not crowd the others out. Its many matching chunks collapse to
    one hit, so the three recalled slots go to three different documents.
    """
    agent = await start_knowledge_agent(deployment, enable_modules)
    try:
        await connect(agent, Provider("wiki", "confluence", "Team wiki", _warranty_pages()))

        await say(agent, "What is the gizmo warranty claim process?")

        prompt = scripted_llm.last_prompt_text
        recalled = [ptr for ptr in pointers_in(prompt) if "/connected/" in ptr]
        assert recalled, "no connected document reached the prompt without a tool call"
        assert len(recalled) == len(set(recalled)), f"a document was recalled twice: {recalled}"
        assert len(recalled) == 3, f"expected three distinct documents, got {recalled}"
        assert "Source: confluence" in prompt and "Link: https://confluence.example.com" in prompt

        # The tool agrees: every document once, the long one not repeated.
        out = await ask(agent, scripted_llm, "document_search", {"query": "gizmo warranty claim"})
        found = pointers_in(out)
        assert len(found) == len(set(found)), found
        assert sorted(titles_in(out)) == sorted(page.title for page in _warranty_pages())
    finally:
        await agent.shutdown()
