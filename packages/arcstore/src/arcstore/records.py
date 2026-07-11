"""Spool record schema — a single flat, frozen model serialized to one JSON line.

One record per arcllm/arcrun/arcagent action. Flat by design (NFR-1): no nested
envelopes, so a record is a single line of JSONL. ``record_id`` is a stable,
content-derived identity used by the store layer for idempotent ingest (FR-3) —
never a byte offset or row id (those change on rotation/compaction).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SpoolKind = Literal["llm_call", "run_event", "agent_event", "tool_event", "spawn_event"]


class SpoolRecord(BaseModel):
    """Immutable operational telemetry record.

    Metadata only by default (SPEC-026 FR-4 / AC-4.5) — no prompt or response
    text. Raw-body capture is an explicit, audited opt-in handled upstream in
    arcllm, never the default here.
    """

    # protected_namespaces=() so the ``model`` field does not collide with
    # Pydantic's ``model_`` reserved prefix.
    model_config = ConfigDict(frozen=True, protected_namespaces=())

    kind: SpoolKind
    """Record category — drives which store table the ingester targets."""

    actor_did: str
    """DID of the entity that produced the action."""

    ts: str | None = None
    """ISO-8601 UTC timestamp. Auto-populated at creation if omitted."""

    request_id: str | None = None
    """Correlation id (typically a UUID) for distributed tracing + idempotency."""

    # llm_call fields
    model: str | None = None
    provider: str | None = None
    """LLM provider name (e.g. ``anthropic``) — llm_call."""
    agent_label: str | None = None
    """Human-readable agent label for UI display — llm_call / run_event."""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # Prompt-caching breakdown (SPEC-029). ``prompt_tokens`` is the SUMMED input
    # context (input + cache_read + cache_write); these two persist the split so
    # a consumer can compute hit-rate = cache_read / (input + cache_read).
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None
    outcome: str | None = None
    """``ok`` | ``error`` | domain-specific."""

    # run_event / agent_event fields
    name: str | None = None
    """Step/phase/event name for run and agent events."""

    # tool_event fields (SPEC-028 FR-1/FR-2) — names align to OTel GenAI semconv
    # (gen_ai.tool.name, gen_ai.operation.name=execute_tool). Digests are of the
    # canonical args/result content (computed at source in arcrun.executor, C1);
    # bodies ride ``extra`` only when store_raw_bodies=true (NFR-2).
    tool_name: str | None = None
    """Tool name for tool_event — code-exec is recognized by this name."""
    phase: str | None = None
    """``start`` | ``end`` | ``error`` for tool_event lifecycle."""
    args_digest: str | None = None
    """sha256 of the canonical tool arguments (content, not length)."""
    args_size: int | None = None
    result_digest: str | None = None
    """sha256 of the canonical tool result (content, not length — C1)."""
    result_size: int | None = None

    # spawn_event fields (SPEC-028 FR-3) — the operational parent→child edge.
    parent_did: str | None = None
    child_did: str | None = None
    role: str | None = None
    depth: int | None = None

    extra: dict[str, Any] = Field(default_factory=dict)
    """Flat key/value extension (str/int/float/bool/None values only)."""

    @model_validator(mode="after")
    def _set_ts(self) -> SpoolRecord:
        # Populate the timestamp at creation if not provided.
        # object.__setattr__ because the model is frozen.
        if self.ts is None:
            object.__setattr__(self, "ts", datetime.now(UTC).isoformat())
        return self

    @property
    def record_id(self) -> str:
        """Stable, content-derived identity for idempotent ingest (FR-3).

        Includes the event discriminators ``phase`` (tool start/end/error) and
        ``name`` (run/agent step) so two distinct events of one run under one
        actor at the same ``ts`` do not collide and silently drop on
        ``INSERT OR IGNORE`` (SPEC-028 review EDGE-3).
        """
        raw = f"{self.kind}|{self.actor_did}|{self.ts}|{self.request_id}|{self.phase}|{self.name}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
