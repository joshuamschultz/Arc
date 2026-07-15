"""Memory tools + the sign->authorize->audit security wrapper (agentic memory).

The agentic "sleep" consolidation loop (``agent_consolidate``) reaches durable
memory ONLY through these tools. Each is a small, single-responsibility operation
over the existing stores (semantic entities, insights, procedures, episodic
stream, the weighted graph, and the retriever) — the same primitives the
deterministic pipeline uses, so an agentic write and a pipeline write land in the
identical glass-box files.

Security (mirrors ``arcagent.core.tool_registry._create_wrapped_execute``): every
tool's ``execute`` is wrapped so that, in order, it

1. builds a :class:`~arctrust.policy.ToolCall` (agent DID, session, classification);
2. ``sign_call`` s it with the memory-agent identity so the pipeline's
   ``IdentityLayer`` can authenticate it — an UNSIGNED call is denied fail-closed;
3. ``await policy_pipeline.evaluate(...)`` — first-DENY-wins, and ANY exception is
   caught and treated as a deny (fail-closed);
4. only on ALLOW runs the underlying store op;
5. emits one :class:`~arctrust.audit.AuditEvent` per call (allow OR deny).

State-modifying tools that cannot be signed+authorized under a configured pipeline
never mutate. Read-only tools may run without a write authorization but STILL emit
audit. This module imports arcrun NOTHING — it speaks a neutral :class:`MemoryTool`
that the single ``react_adapter`` maps onto arcrun.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit
from arctrust.identity import AgentIdentity
from arctrust.policy import PolicyContext, PolicyPipeline, ToolCall, sign_call

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.distill import EntityDisambiguator, confidence_from_hits, resolve_entity
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder
from arcmemory.retrieve import Retriever
from arcmemory.slug import canonical_slug
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Confidence, Insight, Scope, Situation

READ_ONLY = "read_only"
STATE_MODIFYING = "state_modifying"


@dataclass
class MemoryTool:
    """A neutral tool spec — arcrun-agnostic (the adapter maps it onto arcrun).

    ``execute`` takes the validated argument dict and returns a concise string for
    the agent. ``classification`` is ``read_only`` (pure reads parallelize) or
    ``state_modifying`` (writes; sequential, fail-closed without authorization).
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    execute: Callable[[dict[str, Any]], Awaitable[str]]
    classification: str = STATE_MODIFYING


# The underlying, un-wrapped operation: validated args -> concise result string.
_Op = Callable[[dict[str, Any]], Awaitable[str]]


class _MemoryToolFactory:
    """Builds the wrapped memory tools over one agent scope's stores.

    Holds the stores + the security context (identity, policy, audit sink) and
    stamps every tool's ``execute`` with the sign->authorize->audit wrapper.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        db: MemoryDB,
        config: MemoryConfig,
        caller_did: str,
        session_id: str | None,
        identity: AgentIdentity | None,
        policy_pipeline: PolicyPipeline | None,
        audit_sink: AuditSink | None,
        embedder: Embedder | None,
        distiller: EntityDisambiguator | None,
    ) -> None:
        self._workspace = Path(workspace)
        self._db = db
        self._cfg = config
        self._scope = Scope(agent_did=caller_did, session_id=session_id)
        self._identity = identity
        self._pipeline = policy_pipeline
        self._audit = audit_sink if audit_sink is not None else NullSink()
        self._embedder = embedder
        self._distiller = distiller

        self._graph = WeightedGraph(db, config)
        self._semantic = SemanticStore(workspace, self._graph, scope=self._scope.key)
        self._insights = InsightStore(workspace)
        self._procedures = ProceduralStore(workspace)
        self._episodic = EpisodicStore(db, workspace)

    # -- the wrapper -------------------------------------------------------

    def _wrap(self, name: str, classification: str, op: _Op) -> _Op:
        """Stamp one op with sign->authorize->audit (the security template)."""

        async def wrapped(args: dict[str, Any]) -> str:
            authorized, reason = await self._authorize(name, args, classification)
            if not authorized:
                self._emit(name, args, outcome="deny", reason=reason)
                return f"denied: {reason}"
            try:
                result = await op(args)
            except Exception as exc:  # surface the failure; audit it as an error
                self._emit(name, args, outcome="error", reason=str(exc))
                return f"error: {exc}"
            self._emit(name, args, outcome="allow", reason=None)
            return result

        return wrapped

    async def _authorize(
        self, name: str, args: dict[str, Any], classification: str
    ) -> tuple[bool, str]:
        """Sign + evaluate the policy pipeline; fail-closed on deny/exception.

        - No pipeline configured (single-dev degraded path): allow (writes run,
          still audited); this is the ONLY relaxation and only when there is
          genuinely no pipeline.
        - Pipeline configured but no signer: a state-modifying call cannot be
          authenticated -> deny (fail closed). A read-only call may still run.
        - Pipeline + signer: build a signed ToolCall and evaluate; deny or any
          exception fails closed.
        """
        if self._pipeline is None:
            return True, "no-policy-configured"
        if self._identity is None or not self._identity.can_sign:
            if classification == READ_ONLY:
                return True, "read-no-identity"
            return False, "unsigned"
        call = sign_call(
            ToolCall(
                tool_name=name,
                arguments=_json_safe(args),
                agent_did=self._identity.did,
                session_id=self._scope.session_id or "",
                classification=str(args.get("classification", "unclassified")),
            ),
            self._identity,
        )
        ctx = PolicyContext(tier=self._cfg.tier, policy_version="memory", bundle_age_seconds=0.0)
        try:
            decision = await self._pipeline.evaluate(call, ctx)
        except Exception as exc:  # fail-closed — a raising pipeline denies
            return False, f"policy-error:{type(exc).__name__}"
        if decision.is_deny():
            return False, decision.reason or "denied"
        return True, "allow"

    def _emit(
        self, name: str, args: dict[str, Any], *, outcome: str, reason: str | None
    ) -> None:
        """One tamper-evident audit event per memory-tool call (AU-2, federal req)."""
        emit(
            AuditEvent(
                actor_did=self._identity.did if self._identity else self._scope.agent_did,
                action=f"memory.tool.{name}",
                target=str(args.get("slug") or args.get("id") or args.get("src") or name),
                outcome=outcome,
                classification=str(args.get("classification", "unclassified")),
                tier=self._cfg.tier,
                extra={"reason": reason} if reason else {},
            ),
            self._audit,
        )

    # -- read ops ----------------------------------------------------------

    def _retriever(self) -> Retriever:
        return Retriever(
            self._db,
            self._workspace,
            self._scope,
            config=self._cfg,
            embedder=self._embedder,
            audit_sink=self._audit,
        )

    async def _recall_surface(self, args: dict[str, Any]) -> str:
        retriever = self._retriever()
        await retriever.index()
        cards = await retriever.recall_cards(
            Situation(text=str(args.get("query", ""))),
            clearance=self._cfg_clearance(),
            top_k=int(args.get("top_k", 5)),
        )
        return _render_cards(cards) or "(no matches)"

    async def _recall_structural(self, args: dict[str, Any]) -> str:
        cues = [str(c) for c in args.get("cues", [])]
        retriever = self._retriever()
        await retriever.index()
        cards = await retriever.recall_cards(
            Situation(text=" ".join(cues), cues=cues),
            clearance=self._cfg_clearance(),
            top_k=int(args.get("top_k", 5)),
        )
        return _render_cards([c for c in cards if c.kind == "structural"]) or "(no matches)"

    async def _read_card(self, args: dict[str, Any]) -> str:
        entity = self._semantic.read(str(args.get("slug", "")))
        if entity is None:
            return "(no such card)"
        facts = "\n".join(f"- {f.predicate}: {f.value}" for f in entity.facts)
        links = ", ".join(entity.links_to)
        return f"{entity.name} ({entity.entity_type})\n{facts}\nlinks: {links}"

    async def _search_similar_entity(self, args: dict[str, Any]) -> str:
        name = str(args.get("name", ""))
        entity_type = str(args.get("entity_type", "unknown"))
        resolved = await resolve_entity(
            self._semantic,
            slug=canonical_slug(name),
            name=name,
            entity_type=entity_type,
            embedder=self._embedder,
            distiller=self._distiller,
            config=self._cfg,
        )
        exists = self._semantic.read(resolved) is not None
        verdict = "existing" if exists else "new"
        return f"{verdict}: {resolved}"

    async def _neighbors(self, args: dict[str, Any]) -> str:
        slug = canonical_slug(str(args.get("slug", "")))
        pairs = self._graph.neighbors(self._scope.key, slug)
        if not pairs:
            return "(no neighbors)"
        return ", ".join(f"{node} ({weight:.2f})" for node, weight in pairs)

    async def _list_recent_episodes(self, args: dict[str, Any]) -> str:
        limit = int(args.get("limit", 10))
        events = self._episodic.page(self._scope.key, limit=limit, offset=0)
        if not events:
            return "(no episodes)"
        return "\n".join(
            f"- [{e.event_id}] {e.ts[11:16]} ({e.kind}) {e.text[:200]}" for e in events
        )

    # -- write ops ---------------------------------------------------------

    async def _write_fact(self, args: dict[str, Any]) -> str:
        slug = str(args.get("slug", ""))
        name = str(args.get("name") or slug)
        entity_type = str(args.get("entity_type", "unknown"))
        resolved = await resolve_entity(
            self._semantic,
            slug=canonical_slug(slug),
            name=name,
            entity_type=entity_type,
            embedder=self._embedder,
            distiller=self._distiller,
            config=self._cfg,
        )
        self._semantic.write_fact(
            resolved,
            str(args.get("predicate", "")),
            str(args.get("value", "")),
            confidence=float(args.get("confidence", 0.5)),
            name=args.get("name"),
            entity_type=entity_type,
            classification=str(args.get("classification", "unclassified")),
        )
        return f"wrote {resolved}:{args.get('predicate')}"

    async def _merge_entities(self, args: dict[str, Any]) -> str:
        canonical = canonical_slug(str(args.get("canonical", "")))
        other = canonical_slug(str(args.get("other", "")))
        if self._semantic.merge_into(canonical, other):
            self._graph.rename_node(self._scope.key, other, canonical)
            return f"merged {other}->{canonical}"
        return f"no merge ({other}->{canonical})"

    async def _link(self, args: dict[str, Any]) -> str:
        src = canonical_slug(str(args.get("src", "")))
        dst = canonical_slug(str(args.get("dst", "")))
        wrote = self._semantic.add_link(src, dst)
        return f"linked {src}->{dst}" if wrote else f"link exists/absent {src}->{dst}"

    async def _record_insight(self, args: dict[str, Any]) -> str:
        insight_id = canonical_slug(str(args.get("id", "")))
        cues = [str(c) for c in args.get("cues", [])]
        instances = [str(i) for i in args.get("instances", [])]
        # Corroboration: a re-seen insight carries prior hits/cues/instances forward
        # (non-lossy) so confidence accumulates across passes instead of resetting.
        existing = self._insights.read(insight_id)
        hits = (existing.hits if existing else 0) + 1
        if existing is not None:
            cues = list(dict.fromkeys([*existing.cues, *cues]))
            instances = list(dict.fromkeys([*existing.instances, *instances]))
        confidence = confidence_from_hits(hits, self._cfg.gamma)
        status = (
            Confidence.KNOWN if confidence >= self._cfg.known_threshold else Confidence.GUESSED
        )
        insight = Insight(
            id=insight_id,
            statement=str(args.get("statement", "")) or (existing.statement if existing else ""),
            trigger=str(args.get("trigger", "")) or (existing.trigger if existing else ""),
            cues=cues,
            instances=instances,
            classification=str(args.get("classification", "unclassified")),
            confidence=confidence,
            status=status,
            hits=hits,
        )
        self._insights.write(insight)
        for cue in cues:
            self._graph.link(self._scope.key, insight_id, cue, kind="cue")
        return f"recorded insight {insight_id}"

    async def _record_procedure(self, args: dict[str, Any]) -> str:
        slug = canonical_slug(str(args.get("slug", "")))
        steps = [str(s) for s in args.get("steps", [])]
        if not slug or not steps:
            return "skipped (needs slug + steps)"
        self._procedures.upsert(
            slug,
            str(args.get("title", slug)),
            when_to_use=str(args.get("when_to_use", "")),
            steps=steps,
        )
        return f"recorded procedure {slug}"

    async def _set_alias(self, args: dict[str, Any]) -> str:
        slug = canonical_slug(str(args.get("entity", "")))
        alias = str(args.get("alias", "")).strip()
        entity = self._semantic.read(slug)
        if entity is None or not alias:
            return "skipped (unknown entity or empty alias)"
        if alias not in entity.aliases:
            entity.aliases = sorted({*entity.aliases, alias})
            self._semantic.write_fact(
                slug, "alias", alias, confidence=entity.confidence, entity_type=entity.entity_type
            )
        return f"aliased {slug} <- {alias}"

    # -- assembly ----------------------------------------------------------

    def _cfg_clearance(self) -> Any:
        from arctrust.classification import parse_classification

        return parse_classification("unclassified", strict=self._cfg.tier == "federal")

    def build(self) -> list[MemoryTool]:
        """Assemble the wrapped read + write tool set for this scope."""
        reads: list[tuple[str, str, dict[str, Any], _Op]] = [
            (
                "recall_surface",
                "Search memory by text; returns ranked glass-box cards with provenance.",
                _obj({"query": _str(), "top_k": _int()}, required=["query"]),
                self._recall_surface,
            ),
            (
                "recall_structural",
                "Retrieve insights whose abstract cues the situation instances.",
                _obj({"cues": _arr(), "top_k": _int()}, required=["cues"]),
                self._recall_structural,
            ),
            (
                "read_card",
                "Read one entity card (facts + links) by slug.",
                _obj({"slug": _str()}, required=["slug"]),
                self._read_card,
            ),
            (
                "search_similar_entity",
                "Search-before-write: does an entity like this already exist? Returns its slug.",
                _obj({"name": _str(), "entity_type": _str()}, required=["name"]),
                self._search_similar_entity,
            ),
            (
                "neighbors",
                "Graph neighbors of a slug (linked entities/cues with weights).",
                _obj({"slug": _str()}, required=["slug"]),
                self._neighbors,
            ),
            (
                "list_recent_episodes",
                "List recent raw episodes to extract durable memory from.",
                _obj({"limit": _int()}),
                self._list_recent_episodes,
            ),
        ]
        writes: list[tuple[str, str, dict[str, Any], _Op]] = [
            (
                "write_fact",
                "Write one durable fact about an entity (folds into the existing card).",
                _obj(
                    {
                        "slug": _str(),
                        "predicate": _str(),
                        "value": _str(),
                        "confidence": _num(),
                        "name": _str(),
                        "entity_type": _str(),
                        "classification": _str(),
                    },
                    required=["slug", "predicate", "value"],
                ),
                self._write_fact,
            ),
            (
                "merge_entities",
                "Fold a duplicate entity card into the canonical one (non-lossy).",
                _obj({"canonical": _str(), "other": _str()}, required=["canonical", "other"]),
                self._merge_entities,
            ),
            (
                "link",
                "Create a wiki-link edge between two entities.",
                _obj({"src": _str(), "dst": _str()}, required=["src", "dst"]),
                self._link,
            ),
            (
                "record_insight",
                "Mint a reusable insight (mechanism-level trigger + abstract cues).",
                _obj(
                    {
                        "id": _str(),
                        "statement": _str(),
                        "trigger": _str(),
                        "cues": _arr(),
                        "instances": _arr(),
                        "classification": _str(),
                    },
                    required=["id", "statement", "trigger"],
                ),
                self._record_insight,
            ),
            (
                "record_procedure",
                "Record a reusable how-to procedure (title + when_to_use + steps).",
                _obj(
                    {"slug": _str(), "title": _str(), "when_to_use": _str(), "steps": _arr()},
                    required=["slug", "steps"],
                ),
                self._record_procedure,
            ),
            (
                "set_alias",
                "Record an alias on an entity so future writes fold onto it.",
                _obj({"entity": _str(), "alias": _str()}, required=["entity", "alias"]),
                self._set_alias,
            ),
        ]
        tools = [
            MemoryTool(name, desc, schema, self._wrap(name, READ_ONLY, op), READ_ONLY)
            for name, desc, schema, op in reads
        ]
        tools += [
            MemoryTool(name, desc, schema, self._wrap(name, STATE_MODIFYING, op), STATE_MODIFYING)
            for name, desc, schema, op in writes
        ]
        return tools


def build_memory_tools(
    *,
    workspace: Path | str,
    db: MemoryDB,
    config: MemoryConfig,
    caller_did: str,
    session_id: str | None = None,
    identity: AgentIdentity | None = None,
    policy_pipeline: PolicyPipeline | None = None,
    audit_sink: AuditSink | None = None,
    embedder: Embedder | None = None,
    distiller: EntityDisambiguator | None = None,
) -> list[MemoryTool]:
    """Build the wrapped memory tool set over one agent's memory (the agentic surface).

    Every tool carries the sign->authorize->audit wrapper. ``identity`` is the
    memory-agent signing identity (its DID becomes each ToolCall's actor); when a
    ``policy_pipeline`` is configured, state-modifying calls that cannot be
    signed+authorized are denied fail-closed and never mutate.
    """
    return _MemoryToolFactory(
        workspace=Path(workspace),
        db=db,
        config=config,
        caller_did=caller_did,
        session_id=session_id,
        identity=identity,
        policy_pipeline=policy_pipeline,
        audit_sink=audit_sink,
        embedder=embedder,
        distiller=distiller,
    ).build()


# -- tiny JSON-Schema helpers (keep the tool table readable) ----------------


def _str() -> dict[str, Any]:
    return {"type": "string"}


def _int() -> dict[str, Any]:
    return {"type": "integer"}


def _num() -> dict[str, Any]:
    return {"type": "number"}


def _arr() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def _obj(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _json_safe(args: dict[str, Any]) -> dict[str, Any]:
    """Round-trip the args through JSON so the signed payload is canonical + inert."""
    return dict(json.loads(json.dumps(args, default=str)))


def _render_cards(cards: list[Any]) -> str:
    """One concise line per recall card (source · kind · confidence · links)."""
    lines: list[str] = []
    for card in cards:
        links = f" ->[{', '.join(card.links)}]" if card.links else ""
        flag = " (verify)" if card.verify_first else ""
        lines.append(f"- [{card.source}] ({card.kind}){flag} {card.content[:200]}{links}")
    return "\n".join(lines)


__all__ = ["READ_ONLY", "STATE_MODIFYING", "MemoryTool", "build_memory_tools"]
