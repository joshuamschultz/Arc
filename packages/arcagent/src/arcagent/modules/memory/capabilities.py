"""Thin memory wiring — the only arcagent-side memory code (SPEC-041 §4.6, DC-4).

This module holds no memory logic; it wires the config-selected
:class:`~arcagent.brain.Brain` to the agent lifecycle:

* ``capture`` on ``agent:post_tool`` + ``agent:post_respond`` (fast, zero-LLM);
* ``retrieve`` on ``agent:assemble_prompt`` @ priority 50 → ``sections["recall"]``,
  query-conditioned, once-per-turn (spawn double-assembly hits the cache);
* one de-duplicated ``memory_search`` tool;
* ``consolidate`` scheduled by a ``@background_task`` that polls an event-count /
  idle trigger (DC-5) and emits ``memory.consolidated`` for grounded reflection.

Every Brain call is preceded by a generic ACL check: the wiring asks the selected
Brain provider to :meth:`authorize` the operation and honors a denial before touching
the brain (the ACL *policy* lives in the backend; arcagent only asks). With a
:class:`~arcagent.brain.NullBrain` selected, ``state().active`` is ``False`` and every
hook short-circuits — a truly silent no-op that writes nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections.abc import Coroutine, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime
from html import escape
from typing import Any

import arcrun

from arcagent.core import midloop_recall, turn_context
from arcagent.core.errors import CapabilityUnavailableError
from arcagent.core.session_internal.capability_ledger import current_session_id
from arcagent.extension.untrusted import frame_untrusted
from arcagent.knowledge import (
    SHARED_KNOWLEDGE_ATTACHED,
    SHARED_KNOWLEDGE_DETACHED,
    KnowledgeAccess,
    KnowledgeDraft,
    KnowledgePort,
)
from arcagent.modules.memory import _runtime
from arcagent.tools._decorator import background_task, capability, hook, tool
from arcagent.utils.audit import safe_audit

_logger = logging.getLogger("arcagent.modules.memory.capabilities")

_RECALL_PRIORITY = 50
_CAPTURE_PRIORITY = 100
_CONSOLIDATE_POLL_INTERVAL = 300.0


# Told to "remember X" with a NullBrain active, the model will happily reply
# "saved to memory" while nothing persists (ASI09 trust exploitation). There is
# no save-tool to make truthful — capture is an automatic hook that silently
# no-ops — so the honest fix is to tell the model, in the prompt, that durable
# memory is off whenever the brain is inactive.
async def _module_prompt(ctx: Any, st: Any, name: str) -> str:
    """This module's prompt ``arcagent/<name>``: the agent override, else stock.

    Resolved from the run's frozen prompt set carried on the assemble payload, so
    an operator's ArcUI edit reaches the model on the next run and never shifts a
    section mid-run; the module's configured source answers outside a run (file
    read + verify off the loop).
    """
    source = ctx.data.get("prompt_source") or st.prompt_source
    text: str = await asyncio.to_thread(source.resolve, "arcagent", name)
    return text


# -- ACL gating ----------------------------------------------------------


async def _acl_allows(operation: str, caller_did: str) -> bool:
    """Ask the selected Brain provider to authorize a memory operation.

    The generic "ask the provider" seam: the ACL *policy* lives in the backend, so the
    wiring simply calls the Brain's optional ``authorize(operation, caller_did=...)``
    before recall or capture. A backend that exposes no such method (or the NullBrain)
    imposes no gate — allow.
    """
    st = _runtime.state()
    authorize = getattr(st.brain, "authorize", None)
    if authorize is None:
        return True
    return bool(await authorize(operation, caller_did=caller_did))


async def _audit(event: str, detail: dict[str, Any]) -> None:
    await safe_audit(_runtime.state().telemetry, event, detail, logger=_logger)


# -- Context retrieval (the turn's one pre-model retrieval) ----------------


@capability(name="context_retrieval")
class ContextRetrieval:
    """Serves the agent core's Context prep stage: this turn's retrieval candidates.

    Registered under the ``context_retrieval`` operation contract so the core
    reaches it without naming this module (ADR-033). It returns CANDIDATES, not a
    prompt: the core owns the budget, the top-k and token caps, the ranking, the
    rendering and the run trace. Every candidate has already passed the Brain's
    no-read-up gate at ``unclassified``.

    One pass replaces the three that ran before (assembly-time recall, the
    user-turn proactive moments and the pre-respond insight recall): the
    request's cues seed the Brain's graph channel, which is what the entity and
    topic moments contributed, and anything a loop moment staged since the last
    turn is drained in as well.
    """

    async def setup(self, ctx: Any) -> None:
        del ctx

    async def teardown(self) -> None:
        return None

    async def retrieve(
        self, query: str, *, memory_top_k: int, docs_top_k: int
    ) -> Mapping[str, object]:
        """``{"candidates": [...], "steps": [...]}`` for ``query``; best-effort per step.

        The sources are searched concurrently, so the slowest one bounds the
        stage rather than their sum.
        """
        st = _runtime.state()
        if not st.active or not query.strip():
            return {"candidates": [], "steps": []}
        steps: list[dict[str, object]] = []
        memory, documents, profile = await asyncio.gather(
            _timed_step(steps, "memory", _memory_candidates(st, query, memory_top_k)),
            _timed_step(steps, "connections", _document_candidates(st, query, docs_top_k)),
            _timed_step(steps, "profile", _profile_candidates(st)),
        )
        staged = [
            _candidate("memory", f"proactive:{index}", "staged recall", 1.0, "unclassified", text)
            for index, text in enumerate(st.proactive_buffer)
            if text
        ]
        st.proactive_buffer.clear()
        return {"candidates": [*profile, *memory, *documents, *staged], "steps": steps}


async def _timed_step(
    steps: list[dict[str, object]],
    name: str,
    work: Coroutine[Any, Any, list[dict[str, object]]],
) -> list[dict[str, object]]:
    """Run one retrieval step; record its latency and status, never raise."""
    started = time.monotonic()
    status = "ok"
    found: list[dict[str, object]] = []
    try:
        found = await work
    except Exception:  # reason: one failed source must not cost the others
        _logger.warning("context retrieval step %s failed", name, exc_info=True)
        status = "error"
    steps.append(
        {
            "name": name,
            "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
            "status": status,
            "found": len(found),
        }
    )
    return found


def _candidate(
    source_kind: str,
    source: str,
    title: str,
    score: float,
    classification: str,
    text: str,
    path: str = "",
) -> dict[str, object]:
    return {
        "source_kind": source_kind,
        "source": source,
        "title": title,
        "path": path,
        "score": float(score),
        "classification": classification,
        "text": text,
    }


async def _memory_candidates(
    st: _runtime._State, query: str, top_k: int
) -> list[dict[str, object]]:
    """The Brain's gated recall cards for this request (query-only, no corpus embed)."""
    if top_k <= 0:
        return []
    if not await _acl_allows("memory.search", st.agent_did):
        return []
    recall = getattr(st.brain, "recall", None)
    if recall is None:
        return []
    from arcagent.utils.moment import moment_cues

    cards = await recall(
        query,
        clearance="unclassified",
        top_k=top_k,
        budget=st.config.budget,
        cues=moment_cues(query),
        index=False,  # the turn embeds only its query; indexing is background work
    )
    await _audit("memory.recall", {"query_len": len(query), "hit": bool(cards)})
    return [
        _candidate(
            "memory",
            str(card.source),
            str(card.kind),
            float(card.score),
            str(card.classification),
            str(card.content),
        )
        for card in cards
    ]


async def _document_candidates(
    st: _runtime._State, query: str, top_k: int
) -> list[dict[str, object]]:
    """Connected documents (this agent's pools and the shared stores it reads)."""
    if top_k <= 0:
        return []
    search = getattr(st.brain, "document_search", None)
    if search is None:
        return []
    hits = await search(query, top_k=top_k, caller_did=st.agent_did)
    hits = await _with_shared_hits(st, query, list(hits), top_k=top_k)
    return [
        _candidate(
            "connection",
            str(getattr(hit, "source_id", "")),
            str(getattr(hit, "title", "") or getattr(hit, "pointer", "")),
            float(getattr(hit, "score", 0.0)),
            str(getattr(hit, "classification", "unclassified")),
            _doc_blocks([hit])[0][1],
            path=str(getattr(hit, "pointer", "")),
        )
        for hit in hits
    ]


async def _profile_candidates(st: _runtime._State) -> list[dict[str, object]]:
    """Operator-reviewed profile facts for this agent (approved only)."""
    try:
        module = __import__("arcmemory.profile", fromlist=["ProfileReviewStore", "ReviewStatus"])
    except ImportError:
        return []
    store = module.ProfileReviewStore(st.workspace, agent_did=st.agent_did)
    facts = await store.list(status=module.ReviewStatus.APPROVED, profile_id=st.agent_did)
    return [
        _candidate(
            "profile",
            f"profile:{fact.field}",
            str(fact.field),
            1.0,
            "unclassified",
            f"{fact.field}: {fact.value}",
        )
        for fact in facts
    ]


# -- Manual promotion run: "Run now" (SPEC-083 REQ-512) -------------------


@capability(name="memory_promotion")
class MemoryPromotionRun:
    """Serves ``ArcAgent.run_memory_promotion`` — the operator's "Run now".

    Registered under the ``memory_promotion`` operation contract so the agent core
    reaches it without naming this module (ADR-033). Holds no state: every call
    resolves the NAMED agent's Brain (:func:`_runtime.state_for`, fail closed) and
    runs its promotion sweep once. The Brain owns the per-agent lock shared with
    the nightly sweep, the ``max_items`` bounds and every gate.
    """

    async def setup(self, ctx: Any) -> None:
        del ctx

    async def teardown(self) -> None:
        return None

    async def run(self, *, agent_did: str, max_items: int | None = None) -> Mapping[str, object]:
        """Run the named agent's promotion sweep once; its status and counts."""
        run_promotion = _brain_operation(agent_did, "run_promotion")
        return _as_mapping(await run_promotion(max_items=max_items))

    async def share(
        self, *, agent_did: str, kind: str, item_id: str, decided_by: str
    ) -> Mapping[str, object]:
        """Share one of the named agent's cards on an operator's decision (alpha-2 item 16).

        Returns ``{"status", "shared_ref"}`` — never content. The Brain's sweep runs
        every gate (tier, demotion, secret, size, clearance).
        """
        share = _brain_operation(agent_did, "share_memory_item")
        return _as_mapping(await share(kind, item_id, decided_by=decided_by))

    async def history(
        self, *, agent_did: str, kind: str, item_id: str
    ) -> list[Mapping[str, object]]:
        """One card's verified promotion decisions, oldest first (no content).

        Only rows whose signature verified are returned, so the signature itself
        is left off the wire.
        """
        history = _brain_operation(agent_did, "promotion_history")
        rows = await asyncio.to_thread(history, kind, item_id)
        return [
            {key: value for key, value in row.stored().items() if key != "signature"}
            for row in rows
        ]


def _brain_operation(agent_did: str, name: str) -> Any:
    """The NAMED agent's Brain operation; refused when its backend has none."""
    operation = getattr(_runtime.state_for(agent_did).brain, name, None)
    if operation is None:
        raise CapabilityUnavailableError(
            code="MEMORY_PROMOTION_UNAVAILABLE",
            message="this agent's memory backend cannot run promotion",
        )
    return operation


def _as_mapping(result: Any) -> Mapping[str, object]:
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(result)
    return dict(result)


# -- Fleet shared-knowledge port (SPEC-083) -------------------------------


@hook(event=SHARED_KNOWLEDGE_ATTACHED)
async def on_shared_knowledge_attached(ctx: Any) -> None:
    """Hold the fleet's shared-knowledge port the agent core published on its bus.

    Accepted only when the bus certifies the agent core emitted it: every module
    shares the bus, and a forged port would capture each promoted card. A forged
    event is ignored and audited. Resolved by the event's agent DID (never
    ambient), so a port bound for one agent can never land on another's state.
    """
    if not getattr(ctx, "core_certified", False):
        _runtime.refuse_uncertified_shared_knowledge(ctx.agent_did, ctx.event)
        return
    _runtime.attach_shared_knowledge(ctx.agent_did, ctx.data.get("port"))


@hook(event=SHARED_KNOWLEDGE_DETACHED)
async def on_shared_knowledge_detached(ctx: Any) -> None:
    """Drop the fleet's port when the agent core withdraws it (certified events only)."""
    if not getattr(ctx, "core_certified", False):
        _runtime.refuse_uncertified_shared_knowledge(ctx.agent_did, ctx.event)
        return
    _runtime.attach_shared_knowledge(ctx.agent_did, None)


# -- Proactive detected-moment subscriber --------------------------------


@hook(event="agent:moment")
async def on_agent_moment(ctx: Any) -> None:
    """Stage proactive recall for a detected moment into the buffer (SPEC-071).

    A loop site emits ``agent:moment`` with a primitive
    ``{"kind", "cues", "text", "session_id"}`` payload. This subscriber asks the
    Brain's optional ``on_moment`` whether — and what — to recall, and buffers any
    non-empty text on ``_State.proactive_buffer`` for the next prompt assembly to
    drain. Gated on ``active`` + ``proactive_enabled`` so a disabled agent runs
    unchanged (REQ-348), and Brain-agnostic via the ``getattr`` optional-method
    pattern (mirrors :func:`_acl_allows`) so a backend without ``on_moment`` no-ops.
    """
    st = _runtime.state()
    if not st.active or not st.config.proactive_enabled:
        return
    on_moment = getattr(st.brain, "on_moment", None)
    if on_moment is None:
        return

    kind = str(ctx.data.get("kind", ""))
    # decision_point fires mid-loop (pre_plan default / pre_tool opt-in). It cannot inject
    # at prompt assembly (already built), so it is opt-in and routes to the mid-loop channel
    # instead (SPEC-072 COMP-005). Skip unless enabled (SPEC-071 A3); the pre_tool site is a
    # further opt-in on top, honored here since the loop emits both points unconditionally.
    if kind == "decision_point":
        if not st.config.proactive_decision_point:
            return
        point = str(ctx.data.get("point", "pre_plan"))
        if point == "pre_tool" and not st.config.decision_point_pre_tool:
            return
    cues = list(ctx.data.get("cues") or [])
    text = str(ctx.data.get("text", ""))
    session_id = ctx.data.get("session_id")

    text_out = await on_moment(
        kind,
        cues=cues,
        text=text,
        clearance="unclassified",
        top_k=st.config.top_k,
        budget=st.config.budget,
        session_id=session_id,
        index=False,  # query-only on the turn; indexing is the background refresh's job
    )
    if text_out:
        # A decision-point recall rides the mid-loop channel (appended before the next
        # model call by ContextManager.transform_context); every other kind is drained
        # into sections["recall"] at the next prompt assembly.
        if kind == "decision_point":
            midloop_recall.stage(st.agent_did, text_out)
        else:
            st.proactive_buffer.append(text_out)
    await _audit("memory.proactive_recall", {"kind": kind, "hit": bool(text_out)})


@hook(event="agent:assemble_prompt", priority=_RECALL_PRIORITY)
async def inject_memory_disabled_note(ctx: Any) -> None:
    """Tell the model durable memory is off when the brain is inactive (NullBrain).

    Only fires when the memory module is loaded but ``active`` is False (brain
    ``none`` — federal zero-config or explicit). Prevents the "saved to memory"
    over-claim. When a real brain is active this is silent.
    """
    st = _runtime.state()
    if st.active:
        return
    sections = ctx.data.get("sections")
    if isinstance(sections, dict):
        sections["memory_status"] = await _module_prompt(ctx, st, "memory_disabled_note")


# -- Capture hooks -------------------------------------------------------


# Tool events that are pure transcript noise for durable memory. Recalling memory
# and re-capturing the recall is a feedback loop (the "untrusted reference DATA
# retrieved from memory" garbage), and trivial results ("No matches found.",
# "removed", "ok") carry no signal worth distilling.
_NO_CAPTURE_TOOLS = frozenset({"memory_search"})
_MIN_TOOL_RESULT_CHARS = 24


def _worth_capturing_tool(tool_name: str, result: str) -> bool:
    """Whether a tool event carries durable signal (vs. transcript noise)."""
    if tool_name in _NO_CAPTURE_TOOLS:
        return False
    stripped = result.strip()
    if len(stripped) < _MIN_TOOL_RESULT_CHARS:
        return False
    return "untrusted reference data retrieved from memory" not in stripped.lower()


@hook(event="agent:post_tool", priority=_CAPTURE_PRIORITY)
async def capture_tool(ctx: Any) -> None:
    """Capture a tool invocation + result (fast, zero-LLM) — skipping pure noise."""
    st = _runtime.state()
    if not st.active:
        return
    tool_name = str(ctx.data.get("tool", ""))
    from arcagent.tools._secret_guard import redact_tool_event_value

    result = str(redact_tool_event_value(ctx.data.get("result", "")))
    if not _worth_capturing_tool(tool_name, result):
        return
    text = f"tool:{tool_name} -> {result}".strip()
    await _capture(st, text, kind="tool")


@hook(event="agent:pre_respond", priority=_CAPTURE_PRIORITY)
async def capture_user(ctx: Any) -> None:
    """Capture the user's input turn (fast, zero-LLM).

    Without this, memory is built only from tool plumbing and the agent's own
    responses — the human's actual words never enter it. The task text is the
    external input driving the turn; captured as ``user`` so curation keeps it and
    distillation learns from what was asked. Sanitize/dedup happen in the Brain's
    fast path (untrusted input, LLM01), same as every other capture.
    """
    st = _runtime.state()
    if not st.active:
        return
    if turn_context.overheard():
        # A channel post addressed to nobody wakes every member agent. Each may
        # answer it; none may keep it. Retaining here is how one operator message
        # to one agent ends up permanently in every other agent's memory.
        return
    text = str(ctx.data.get("task", "")).strip()
    await _capture(st, text, kind="user")


@hook(event="agent:post_respond", priority=_CAPTURE_PRIORITY)
async def capture_respond(ctx: Any) -> None:
    """Capture the assistant's response turn (fast, zero-LLM)."""
    st = _runtime.state()
    if not st.active:
        return
    messages = ctx.data.get("messages", [])
    text = "\n".join(
        arcrun.content_text(m.get("content")) for m in messages if isinstance(m, dict)
    ).strip()
    await _capture(st, text, kind="respond")


async def _capture(st: _runtime._State, text: str, *, kind: str) -> None:
    """ACL-gated Brain capture + consolidation-trigger bookkeeping.

    Capture always records the content. The pending-events counter, though, only
    advances on a turn a real person drove (``turn_context.interactive()``): the
    agent's own background machinery — the pulse tick, the proactive scheduler,
    consolidation itself — runs turns constantly, and counting those made a quiet
    agent look like it always had new memory, re-embedding the index for nothing.
    The counter now gates only the index refresh; consolidation runs once per night
    (see :func:`consolidate_poll_once`), not on this counter.
    """
    if not text:
        return
    if not await _acl_allows("memory.write", st.agent_did):
        return
    await st.brain.capture(text, kind=kind)
    if turn_context.interactive():
        st.events_since_consolidate += 1
        st.last_activity = time.monotonic()
    await _audit("memory.capture", {"kind": kind})
    await _announce_ingest(st, text, kind)


async def _announce_ingest(st: _runtime._State, text: str, kind: str) -> None:
    """Tell the bus something was filed, so a listener can publish a pointer.

    This module owns private memory and must not know that a team, a channel or
    a published digest exists — so it says *what happened* and nothing about who
    should care (ADR-032, Non-Negotiable 2). The messaging module listens and
    decides what, if anything, to publish about it.

    Announcing is best-effort: a listener's failure is not a reason to lose the
    memory that was already captured.
    """
    if st.bus is None:
        return
    try:
        await st.bus.emit("memory:captured", {"text": text, "kind": kind})
    except Exception:  # reason: a downstream listener must not break capture
        _logger.debug("memory:captured listener raised; capture stands", exc_info=True)


# -- Digest backfill -----------------------------------------------------


@hook(event="agent:ready", priority=90)
async def backfill_digest_from_holdings(_ctx: Any) -> None:
    """Seed the channel-routing digest from what memory already holds (once).

    The digest is written at ingest, so an agent that filed knowledge *before*
    the digest existed is invisible to the router until it re-files it — which is
    how an agent holding the answer never even tried. Replaying its durable
    holdings through the same ``memory:captured`` seam the live capture path uses
    lets the messaging module publish a pointer for each, with no new coupling
    between the two modules. Best-effort: a backfill must never break startup.
    """
    st = _runtime.state()
    if not st.active or st.digest_backfilled or st.bus is None:
        return
    st.digest_backfilled = True
    enumerate_holdings = getattr(st.brain, "holdings", None)
    if enumerate_holdings is None:
        return
    try:
        items = await enumerate_holdings()
    except Exception:  # reason: a routing backfill must never break agent startup
        _logger.warning("could not enumerate holdings for digest backfill", exc_info=True)
        return
    for text in items:
        await _announce_ingest(st, text, "observation")


# -- memory_search tool --------------------------------------------------


@tool(
    name="memory_search",
    description="Search agent memory across observations, entities, and insights.",
    classification="read_only",
    when_to_use="Recall facts, past conversations, procedures, or learned insights.",
)
async def memory_search(query: str, top_k: int = 5) -> str:
    """Query-conditioned recall, boundary-marked (LLM01). Empty when memory is off."""
    st = _runtime.state()
    if not st.active:
        return "Memory is not enabled for this agent."
    if not await _acl_allows("memory.search", st.agent_did):
        return "No memory results found."
    text = await st.brain.retrieve(query, clearance="unclassified", top_k=top_k)
    await _audit("memory.recall", {"query_len": len(query), "hit": bool(text), "tool": True})
    return text or "No memory results found."


# -- Explicit curated knowledge tools ------------------------------------


def _personal_knowledge_port() -> tuple[KnowledgePort, KnowledgeAccess]:
    """Return the local curated-knowledge port and trusted caller context."""
    st = _runtime.state()
    if st.knowledge_access is None or st.personal_knowledge is None:
        raise RuntimeError("curated knowledge is not enabled for this agent")
    return st.personal_knowledge, st.knowledge_access


@tool(
    name="knowledge_save",
    description="Save a curated document in this agent's personal workspace.",
    classification="state_modifying",
    capability_tags=["knowledge", "memory"],
    when_to_use="Persist a reviewed fact, procedure, or note for this agent.",
)
async def knowledge_save(
    title: str,
    content: str,
    classification: str = "UNCLASSIFIED",
    tags: list[str] | None = None,
    document_type: str = "note",
) -> str:
    """Save exactly one local curated document."""
    try:
        port, access = _personal_knowledge_port()
        reference = await port.save(
            KnowledgeDraft(title, content, classification, tuple(tags or ()), document_type),
            access,
        )
    except (PermissionError, RuntimeError, ValueError) as error:
        return f"Knowledge save refused: {error}"
    return f"Saved personal knowledge {reference.identifier}."


@tool(
    name="knowledge_retrieve",
    description="Retrieve one document from this agent's personal curated-knowledge workspace.",
    classification="read_only",
    capability_tags=["knowledge", "memory"],
    when_to_use="Read a known personal curated document.",
)
async def knowledge_retrieve(reference: str) -> str:
    """Return a boundary-marked, clearance-checked document."""
    st = _runtime.state()
    try:
        port, access = _personal_knowledge_port()
        document = await port.read(reference, access)
    except (FileNotFoundError, PermissionError, RuntimeError, ValueError) as error:
        return f"Knowledge retrieval refused: {error}"
    rendered, truncated, defanged = _render_knowledge_document(st, "personal", document)
    await _audit(
        "memory.knowledge_retrieve",
        {
            "scope": "personal",
            "reference": document.reference.identifier,
            "content_chars": len(document.content),
            "rendered_chars": len(rendered),
            "truncated": truncated,
            "boundary_markers_defanged": defanged,
        },
    )
    return rendered


_MAX_KNOWLEDGE_BUDGET = 16_384


def _render_knowledge_document(
    st: _runtime._State, scope: str, document: Any
) -> tuple[str, bool, bool]:
    """Sanitize and bound one document for prompt presentation only.

    Curated OKF content is integrity-checked by its adapter before this function is
    called.  The sanitizer therefore applies only to this rendered copy: the exact
    stored body remains available to the adapter and its digest check.  ``budget`` is
    a deterministic token estimate (~4 characters/token), matching arcmemory's
    general-memory retrieval budget.
    """
    prefix = (
        f'<knowledge-document scope="{scope}" '
        f'reference="{escape(document.reference.identifier)}">\n'
        f"# {escape(document.title)}\n\n"
    )
    suffix = "\n</knowledge-document>"
    requested_budget = st.config.knowledge_budget or st.config.budget
    budget = min(max(requested_budget, 1), _MAX_KNOWLEDGE_BUDGET)
    # Reserve the wrapper before applying the character cap so the complete output
    # stays below the same approximate token budget used by general memory recall.
    body_chars = max(0, (budget * 4) - len(prefix) - len(suffix))
    try:
        from arcmemory.security import document_sanitize
    except ImportError:  # pragma: no cover - curated ports require arcmemory
        clean = document.content[:body_chars]
    else:
        clean = document_sanitize(document.content, max_length=body_chars)
    rendered = f"{prefix}{clean}{suffix}"
    return rendered, len(clean) < len(document.content), clean != document.content


@tool(
    name="knowledge_search",
    description="Search this agent's personal curated-knowledge workspace.",
    classification="read_only",
    capability_tags=["knowledge", "memory"],
    when_to_use="Find personal curated knowledge.",
)
async def knowledge_search(query: str) -> str:
    """Search local curated knowledge and return only clearance-permitted hits."""
    try:
        port, access = _personal_knowledge_port()
        hits = await port.search(query, access)
    except (PermissionError, RuntimeError, ValueError) as error:
        return f"Knowledge search refused: {error}"
    if not hits:
        return "No knowledge results found."
    return "\n".join(f"- {hit.reference.identifier}: {hit.title} — {hit.excerpt}" for hit in hits)


@tool(
    name="document_search",
    description=(
        "Search the full text of every document source the operator connected — "
        "wiki pages, tracker issues, mail, files in cloud storage, repository files."
    ),
    classification="read_only",
    when_to_use=(
        "ALWAYS before answering that something is undocumented, not written down, "
        "or does not exist. Memory holds what you were told; this holds what the "
        "organisation actually wrote, which is far more. Omit source to search "
        "everything, or pass a name from connected_sources to narrow it."
    ),
)
async def document_search(query: str, source: str | None = None, top_k: int | None = None) -> str:
    """Query connected document sources, boundary-marked. Graceful when none is wired."""
    st = _runtime.state()
    if not st.active:
        return "Memory is not enabled for this agent."
    search = getattr(st.brain, "document_search", None)
    if search is None:
        return "Document search is not available for this agent."
    if not await _acl_allows("memory.search", st.agent_did):
        return "No document results found."
    scope: dict[str, Any] = {}
    if source:
        resolved, available = await _resolve_document_sources(source)
        if resolved is None and available:
            return f"No connected source is named {source!r}. Connected sources: {available}."
        scope = {"source_ids": resolved} if resolved is not None else {"source_id": source}
    hits = await search(query, top_k=top_k, caller_did=st.agent_did, **scope)
    wanted = scope.get("source_ids") or ([scope["source_id"]] if "source_id" in scope else None)
    hits = await _with_shared_hits(st, query, hits, top_k=top_k, source_ids=wanted)
    await _audit(
        "memory.document_search",
        {"query_len": len(query), "source": source or "", "hit": bool(hits), "tool": True},
    )
    touched = {str(getattr(hit, "source_id", "")) for hit in hits} | set(wanted or ())
    guides = await _operator_guides(sorted(touched - {""}))
    return guides + _render_doc_hits(query, hits)


async def _operator_guides(source_ids: list[str] | None) -> str:
    """The operator's verified guides for the sources a tool touched (all when ``None``).

    The connected-data service owns which guides this agent may see and hands
    each one over once per run, framed as operator navigation guidance. No
    connected-data module means no guides.
    """
    try:
        runtime = __import__("arcagent.modules.connected_data._runtime", fromlist=["state"])
        service = runtime.state().service
    except RuntimeError:
        return ""
    if service is None:
        return ""
    text: str = await service.guide_context(source_ids=source_ids, run_key=_guide_run_key())
    return text


def _guide_run_key() -> str:
    """What "once per run" means for a guide: the arcrun run, else the session.

    Outside both (a direct call from a test or a script) every call is its own
    run, so a guide is never withheld for having been shown to someone else.
    """
    run_id = arcrun.current_run_id()
    if run_id:
        return f"run:{run_id}"
    session = current_session_id()
    return f"session:{session}" if session else f"call:{os.urandom(8).hex()}"


async def _with_shared_hits(
    st: _runtime._State,
    query: str,
    hits: list[Any],
    *,
    top_k: int | None,
    source_ids: list[str] | None = None,
) -> list[Any]:
    """Merge this agent's hits with the shared connection stores it reads (P18-4).

    A connection several agents are granted is synced once into one store; each
    agent reads it through its own subscription, checked by the connected-data
    service on every call. Best hit first; ``top_k`` bounds the merged list (or,
    when unset, the longer of the two the stores returned on their own).
    """
    try:
        runtime = __import__("arcagent.modules.connected_data._runtime", fromlist=["state"])
        service = runtime.state().service
    except RuntimeError:
        return hits
    search = getattr(service, "shared_document_search", None)
    if search is None:
        return hits
    shared = await search(query, caller_did=st.agent_did, source_ids=source_ids, top_k=top_k)
    if not shared:
        return hits
    limit = top_k if top_k is not None else max(len(hits), len(shared))
    merged = sorted(
        [*hits, *shared], key=lambda hit: float(getattr(hit, "score", 0.0)), reverse=True
    )
    return merged[:limit]


_SOURCE_ID = re.compile(r"^[0-9a-f]{64}$")


async def _resolve_document_sources(name: str) -> tuple[list[str] | None, str]:
    """Map a display name, kind or connection name to its document-pool source ids.

    The model only ever sees names; pools are keyed by a sha256 source id. Returns
    ``(ids, "")`` on a match (several when two accounts share a kind), ``(None, "")``
    when no connected-data service is wired (the caller then passes the value
    through), and ``(None, names)`` when a service exists but nothing matches.
    """
    if _SOURCE_ID.match(name):
        return [name], ""
    try:
        runtime = __import__("arcagent.modules.connected_data._runtime", fromlist=["state"])
        service = runtime.state().service
    except RuntimeError:
        return None, ""
    if service is None:
        return None, ""
    wanted = name.strip().casefold()
    ids: list[str] = []
    names: list[str] = []
    for status in await service.list_sources():
        described = status.description
        if described is None or not status.source_id:
            continue
        label = described.display_name or described.source_kind
        names.append(f"{label} ({described.source_kind})")
        keys = (described.display_name, described.source_kind, status.connection_id)
        if wanted in {str(key).casefold() for key in keys if key}:
            ids.append(status.source_id)
    return (ids, "") if ids else (None, ", ".join(names) or "none")


def _citation_line(hit: Any) -> str:
    """One provenance line a model can cite: title, source, link, last update."""
    parts = [
        ("Title", getattr(hit, "title", "")),
        ("Source", getattr(hit, "source_kind", "")),
        ("Link", getattr(hit, "url", "")),
        ("Updated", getattr(hit, "updated_at", "")),
    ]
    return " | ".join(f"{label}: {value}" for label, value in parts if value)


def _doc_blocks(hits: list[Any]) -> list[tuple[str, str]]:
    """``(pointer, text)`` pairs with each hit's citation line ahead of its text."""
    blocks: list[tuple[str, str]] = []
    for hit in hits:
        citation = _citation_line(hit)
        text = getattr(hit, "text", "")
        blocks.append((getattr(hit, "pointer", ""), f"{citation}\n{text}" if citation else text))
    return blocks


def _render_doc_hits(query: str, hits: list[Any]) -> str:
    """Render document hits as boundary-marked, provenance-carrying DATA."""
    if not hits:
        return f"No document results found for {query!r}."
    return frame_untrusted(_doc_blocks(hits))


@tool(
    name="datastore_query",
    description=("Exact lookup in a connected structured datastore (get_record/find/list)."),
    classification="read_only",
    when_to_use=(
        "Exact lookup in a connected structured datastore — get_record/find/list; "
        "never fuzzy search. Pass the source, the operation, and the table."
    ),
)
async def datastore_query(
    source: str | None,
    op: str,
    table: str,
    args: dict[str, Any] | None = None,
) -> str:
    """Query a connected structured datastore. Graceful when none is wired."""
    st = _runtime.state()
    if not st.active:
        return "Memory is not enabled for this agent."
    query = getattr(st.brain, "datastore_query", None)
    if query is None:
        return "Datastore query is not available for this agent."
    if not await _acl_allows("memory.search", st.agent_did):
        return "No datastore results found."
    sources = (source,) if source else await _approved_datastore_sources()
    results = [
        await query(source_id, op, table, args or {}, caller_did=st.agent_did)
        for source_id in sources
    ]
    result = [item for item in results if item is not None]
    await _audit(
        "memory.datastore_query",
        {"source": source or "auto", "op": op, "table": table, "hit": bool(result), "tool": True},
    )
    context = await _table_meaning_context(st, sources, table)
    guides = await _operator_guides(list(sources))
    return guides + context + _render_datastore_result(result)


async def _table_meaning_context(st: Any, sources: tuple[str, ...], table: str) -> str:
    """Prepend the operator's semantic-layer meaning for ``table`` — COMPOSE, never
    shadow or replace the row data that follows it (H-025 describe-before-query).

    Best-effort: no describe seam, nothing registered, or a signed layer that
    fails integrity verification all return no context rather than blocking the
    row query that already ran — a meaning-composition failure must not hide
    data the caller is otherwise cleared to see.
    """
    describe = getattr(st.brain, "describe_datastore", None)
    if describe is None:
        return ""
    for source_id in sources:
        try:
            text = await describe(source_id, table=table, caller_did=st.agent_did)
        except RuntimeError as exc:  # reason: a signed semantic layer failed
            # verification (tamper) — fail closed on the MEANING, not the rows.
            await _audit(
                "memory.datastore_describe_tampered",
                {"source": source_id, "table": table, "tool": True},
            )
            return f"(semantic layer for {source_id!r} failed integrity verification: {exc})\n\n"
        if text:
            return f"Table meaning (operator's semantic layer):\n{text}\n\n"
    return ""


async def _approved_datastore_sources() -> tuple[str, ...]:
    """Resolve approved datastore ids internally so the model never guesses hashes."""
    try:
        runtime = __import__("arcagent.modules.connected_data._runtime", fromlist=["state"])
        service = runtime.state().service
    except RuntimeError:
        return ()
    if service is None:
        return ()
    candidates = await service.list_sources()
    approved: list[str] = []
    for candidate in candidates:
        if candidate.description is None or candidate.description.source_kind != "postgres":
            continue
        proposal = await service.get_mapping_proposal(candidate.connection_id)
        if proposal is not None and proposal.approval_status == "approved" and candidate.source_id:
            approved.append(candidate.source_id)
    return tuple(approved)


@tool(
    name="datastore_describe",
    description=(
        "Describe a connected structured datastore's tables and columns in the "
        "operator's own words — entity names, meanings, and bounded example values."
    ),
    classification="read_only",
    when_to_use=(
        "ALWAYS before your first datastore_query against a source you have not "
        "already described this session, and again whenever a table name is "
        "unfamiliar. A column called 'amt' means nothing on its own; this is "
        "where an operator has already said what it holds. Omit table to see "
        "every visible table, or pass one from a prior datastore_describe/query."
    ),
)
async def datastore_describe(source: str | None = None, table: str | None = None) -> str:
    """Describe-before-query: the semantic layer's meaning, not the rows themselves."""
    st = _runtime.state()
    if not st.active:
        return "Memory is not enabled for this agent."
    describe = getattr(st.brain, "describe_datastore", None)
    if describe is None:
        return "Datastore description is not available for this agent."
    if not await _acl_allows("memory.search", st.agent_did):
        return "No datastore description found."
    sources = (source,) if source else await _approved_datastore_sources()
    if not sources:
        return "No connected datastore found."
    blocks: list[str] = []
    for source_id in sources:
        try:
            text = await describe(source_id, table=table, caller_did=st.agent_did)
        except RuntimeError as exc:  # reason: signed layer failed verification (tamper)
            await _audit(
                "memory.datastore_describe_tampered",
                {"source": source_id, "table": table or "", "tool": True},
            )
            blocks.append(f"{source_id}: failed integrity verification ({exc})")
            continue
        if text:
            blocks.append(text)
    await _audit(
        "memory.datastore_describe",
        {"source": source or "auto", "table": table or "", "hit": bool(blocks), "tool": True},
    )
    guides = await _operator_guides(list(sources))
    return guides + ("\n\n".join(blocks) if blocks else "No datastore description found.")


@tool(
    name="connected_sources",
    description=(
        "List the accounts the operator connected — the wikis, trackers, mailboxes, "
        "file stores and databases whose contents you can search."
    ),
    classification="read_only",
    when_to_use=(
        "When you do not know what the operator has connected, or need a source "
        "name to narrow document_search. Worth checking before concluding that "
        "something is not available to you."
    ),
)
async def connected_sources() -> str:
    """Show safe connected-source capabilities without returning credentials or ids."""
    try:
        runtime = __import__("arcagent.modules.connected_data._runtime", fromlist=["state"])
        service = runtime.state().service
    except RuntimeError:
        service = None
    if service is None:
        return "No connected sources are available."
    descriptions = [
        f"- {entry.name}: {entry.kind}; status={entry.status}; homes={entry.homes_text}"
        for entry in await service.catalog_entries(refresh=True)
    ]
    if not descriptions:
        return "No connected sources are available."
    return await _operator_guides(None) + "\n".join(descriptions)


@tool(
    name="profile_context",
    description="Read approved profile context; pending suggestions are excluded.",
    classification="read_only",
)
async def profile_context(profile_id: str | None = None) -> str:
    """Return approved profile context only, data-framed before it reaches the model."""
    st = _runtime.state()
    if not st.active:
        return "Memory is not enabled for this agent."
    target = profile_id or st.agent_did
    try:
        module = __import__("arcmemory.profile", fromlist=["ProfileReviewStore", "ReviewStatus"])
    except ImportError:
        return "Profile context is not available for this agent."
    facts = await module.ProfileReviewStore(st.workspace, agent_did=st.agent_did).list(
        status=module.ReviewStatus.APPROVED, profile_id=target
    )
    return frame_untrusted(
        [(f"profile:{fact.field}", f"{fact.field}: {fact.value}") for fact in facts]
    )


def _render_datastore_result(result: object) -> str:
    """Render a typed datastore result (dict/list/None) as boundary-marked DATA.

    Datastore rows are untrusted external content — they are DATA-framed (LLM01)
    before reaching the model, exactly like document hits and memory recalls, rather
    than concatenated raw into the tool's reply.
    """
    if result is None:
        return "No datastore results found."
    rows = result if isinstance(result, list) else [result]
    if not rows:
        return "No datastore results found."
    return frame_untrusted([("datastore", str(row)) for row in rows])


@hook(event="agent:assemble_prompt", priority=_RECALL_PRIORITY)
async def inject_procedure_guidance(ctx: Any) -> None:
    """Point the agent at its procedures — having the tools is not the same as using them.

    A model with a hundred tools does not call one it was never pointed at: an agent
    holding 36 recorded procedures answered from a skill and never opened the matching
    playbook. Injected whenever memory is live, including when the store is still
    empty, because the "list before you record" half is what prevents the duplicates
    in the first place.
    """
    st = _runtime.state()
    if not st.active:
        return
    sections = ctx.data.get("sections")
    if isinstance(sections, dict):
        # Identical on every turn, so it sits in the cached prefix, not re-billed per turn.
        sections["procedures"] = await _module_prompt(ctx, st, "memory_procedure_guidance")


# -- procedure tools ------------------------------------------------------


@tool(
    name="procedure_list",
    description=(
        "List the operator's recorded procedures — slug, title and the situation each "
        "answers to. Steps are NOT included; read the one that matches."
    ),
    classification="read_only",
    when_to_use=(
        "BEFORE doing any recurring task the operator may already have a way of doing, "
        "and before recording a new procedure (so an existing one is updated, not "
        "duplicated). Cheap: triggers only, never the steps."
    ),
)
async def procedure_list() -> str:
    """The procedure index — cheap enough to consult whenever a task might have one."""
    st = _runtime.state()
    if not st.active:
        return "Memory is not enabled for this agent."
    if not await _acl_allows("memory.search", st.agent_did):
        return "(no procedures recorded)"
    text = await st.brain.list_procedures()
    await _audit("memory.procedure_listed", {"tool": True})
    return text


@tool(
    name="procedure_get",
    description=(
        "Read one recorded procedure in full: its trigger and its numbered steps. "
        "Reading it records that it was used."
    ),
    classification="read_only",
    when_to_use=(
        "After procedure_list shows a procedure covering the task at hand — to follow "
        "it, to check every step was done, or to see its current steps before updating it."
    ),
)
async def procedure_get(slug: str) -> str:
    """One playbook in full. Reading is the use, which is how usage gets measured."""
    st = _runtime.state()
    if not st.active:
        return "Memory is not enabled for this agent."
    if not await _acl_allows("memory.search", st.agent_did):
        return f"(no procedure {slug!r})"
    text = await st.brain.get_procedure(slug)
    await _audit("memory.procedure_used", {"slug": slug, "tool": True})
    return text


# -- Consolidation scheduler ---------------------------------------------


#: Operator kill switch for the whole sleep loop — index refresh AND
#: consolidation. Set ``ARC_MEMORY_CONSOLIDATE_OFF=1`` (in the fleet's arc.env)
#: to stop every agent from embedding or consolidating, e.g. while the sleep path
#: is being repaired, without disabling capture or recall. Read per tick so it
#: takes effect at the next poll without a code change.
_CONSOLIDATE_OFF_ENV = "ARC_MEMORY_CONSOLIDATE_OFF"


def _consolidation_off() -> bool:
    """Whether the operator has disabled the sleep loop via the environment."""
    return os.environ.get(_CONSOLIDATE_OFF_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


@background_task(name="memory_consolidate_loop", interval=_CONSOLIDATE_POLL_INTERVAL)
async def memory_consolidate_loop(_ctx: Any) -> None:
    """Poll the event-count / idle trigger; consolidate when it fires (DC-5)."""
    while True:
        if not _consolidation_off():
            try:
                await refresh_index_once()
                await consolidate_poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # reason: fail-open — a sleep-path error must not crash the agent
                _logger.warning("memory consolidation poll failed", exc_info=True)
        await asyncio.sleep(_CONSOLIDATE_POLL_INTERVAL)


async def refresh_index_once() -> None:
    """Keep the surface index warm off the turn path — query-only recall's partner.

    Recall on a turn never embeds the corpus (``retrieve(index=False)``); this
    background pass embeds the changed chunks so newly captured memory becomes
    searchable within a poll interval instead of on a person's turn. It runs when
    there is pending real activity, and once at startup to warm a cold/empty index.
    Content-hash gated, so a warm index with nothing changed costs no embeds.
    """
    st = _runtime.state()
    if not st.active:
        return
    if st.index_warmed and st.events_since_consolidate <= 0:
        return
    await st.brain.refresh_index()
    st.index_warmed = True


#: How long the doc-embed backfill loop waits when there is nothing it can do
#: (no backfill on this brain, the kill switch is on, or a tick failed).
_DOC_BACKFILL_IDLE_SECONDS = 300.0


@background_task(name="memory_doc_embed_backfill", interval=_DOC_BACKFILL_IDLE_SECONDS)
async def memory_doc_embed_backfill_loop(_ctx: Any) -> None:
    """Give this agent's document pools the vectors an embedder outage left missing.

    Connected-data syncs and pushed records alike are embedded once, at write
    time; any written while the embedder could not serve are stored lexical-only.
    Every one of the agent's doc pools lives in arcmemory's single doc-pool store,
    so this one loop covers them all (shared stores are the connected-data
    service's). The Brain owns the retry
    (bounded ticks, background embed lane, backoff while the embedder is down);
    this loop only drives it off the turn path and sleeps what each tick asks.
    Obeys the same kill switch as the sleep loop: it is embedding work.
    """
    while True:
        delay = _DOC_BACKFILL_IDLE_SECONDS
        if not _consolidation_off():
            try:
                delay = await doc_embed_backfill_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # reason: fail-open — a backfill error must not crash the agent
                _logger.warning("memory doc-embed backfill tick failed", exc_info=True)
        await asyncio.sleep(delay)


async def doc_embed_backfill_once() -> float:
    """One bounded backfill tick of this agent's document pools; seconds to wait next."""
    st = _runtime.state()
    maintain = getattr(st.brain, "maintain_doc_embeddings", None)
    if not st.active or not callable(maintain):
        return _DOC_BACKFILL_IDLE_SECONDS
    return float(await maintain())


def _agent_minute_offset(agent_did: str) -> int:
    """A stable 0-59 minute offset from the DID, so the fleet's nightly passes do
    not all call the model at the same instant."""
    return int(hashlib.sha256(agent_did.encode("utf-8")).hexdigest(), 16) % 60


def _in_nightly_window(st: Any, now_local: datetime | None = None) -> bool:
    """Whether local time is inside this agent's quiet nightly window.

    The window opens at ``consolidate_hour`` (local) and stays open for
    ``consolidate_window_hours``, so a brief downtime at the exact hour does not
    skip the night. A per-agent minute offset delays the open within the first hour
    to stagger the fleet. arcmemory still runs the heavy pass at most once per local
    day, so every poll after the first inside the window is a cheap no-op.
    """
    now_local = now_local or datetime.now().astimezone()
    start = st.config.consolidate_hour
    end = start + st.config.consolidate_window_hours
    if not (start <= now_local.hour < end):
        return False
    if now_local.hour == start and now_local.minute < _agent_minute_offset(st.agent_did):
        return False
    return True


async def consolidate_poll_once(*, now_local: datetime | None = None) -> bool:
    """Run the nightly consolidation iff we are in this agent's quiet window.

    Consolidation is a background "sleep" — a full-backlog window review plus dedup,
    a heavy multi-call model pass. It runs ONCE PER NIGHT per agent, inside the quiet
    early-morning window (never on an interactive turn, never every few minutes).
    arcmemory gates the heavy work to once per local day, so repeated polls inside
    the window are no-ops; ``now_local`` is injectable for tests.
    """
    st = _runtime.state()
    if not st.active or not _in_nightly_window(st, now_local):
        return False

    result = await st.brain.consolidate()
    st.events_since_consolidate = 0
    st.last_consolidate_at = time.monotonic()
    await _audit("memory.consolidated", {"summary": str(result.get("episode_summary", ""))})
    if st.bus is not None:
        await st.bus.emit(
            "memory.consolidated",
            {
                "episode_summary": str(result.get("episode_summary", "")),
                "insights_minted": result.get("insights_minted", 0),
                "facts_updated": result.get("facts_updated", 0),
                # The nightly promotion sweep's typed status (SPEC-083); None when
                # the brain ran no sweep.
                "promotion_status": result.get("promotion_status"),
            },
            agent_did=st.agent_did,
        )
    return True


__all__ = [
    "ContextRetrieval",
    "backfill_digest_from_holdings",
    "capture_respond",
    "capture_tool",
    "capture_user",
    "connected_sources",
    "consolidate_poll_once",
    "datastore_describe",
    "datastore_query",
    "document_search",
    "inject_memory_disabled_note",
    "memory_consolidate_loop",
    "memory_search",
    "on_agent_moment",
    "on_shared_knowledge_attached",
    "on_shared_knowledge_detached",
    "profile_context",
]
