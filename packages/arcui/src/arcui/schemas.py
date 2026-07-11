"""Typed response models for arcui routes (Phase 6 §9.1).

Pure refactor. Each model mirrors the existing dict shape returned by a
route handler 1:1 — same field names, same types, same defaults. Field
order in the model definition matches the order of keys in the original
dict literal so ``model_dump(mode="json")`` produces a structurally
identical dict that JSONResponse re-serializes byte-identically.

**Wire-identity contract:**
- No extra fields, no rename, no validation tightening.
- Optional/nullable fields use ``| None = None`` to capture both
  branches when a route returned a variant shape under different
  conditions.
- The byte-identity contract tests (``tests/test_schemas_byte_identity.py``)
  freeze a fixed input → fixed JSON bytes mapping for every model.

**Out of scope (intentional):**
- Routes that return purely passthrough dicts produced by upstream
  components (e.g. ``aggregator.stats(window)``,
  ``cfg.model_dump()`` for arbitrary config snapshots) are NOT
  modelled here. Their shape is owned by the producer; tightening
  it at the wire boundary risks the producer changing shape and
  silently dropping fields. Those routes still pass dicts directly
  into ``JSONResponse``.
- WebSocket message shapes are out of scope (this file covers HTTP
  response bodies only).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Generic / shared
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Single-key error envelope used by every 4xx/5xx route response.

    Matches the dict shape ``{"error": <message>}`` used uniformly
    across all arcui routes.
    """

    model_config = ConfigDict(extra="forbid")

    error: str


# ---------------------------------------------------------------------------
# Agent detail — config / files
# ---------------------------------------------------------------------------


class FilesTreeEntry(BaseModel):
    """One entry in the files-tree listing — file or directory."""

    model_config = ConfigDict(extra="forbid")

    path: str
    type: str
    size: int
    mtime: float


class FilesTreeResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/files/tree``."""

    model_config = ConfigDict(extra="forbid")

    root: str
    entries: list[FilesTreeEntry]


class FileReadResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/files/read``."""

    model_config = ConfigDict(extra="forbid")

    path: str
    size: int
    mtime: float
    content: str
    content_type: str


class FileWriteResponse(BaseModel):
    """Body of ``PUT /api/agents/{id}/files/read`` (COMP-012 / REQ-099).

    ``signature_stale`` is True when the saved file has an ``.arcsig`` sidecar
    that the write invalidated — the UI holds no agent identity and cannot
    re-sign, so ``message`` tells the operator the agent must.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    size: int
    mtime: float
    signature_stale: bool
    message: str


# ---------------------------------------------------------------------------
# Agent detail — sessions / tasks / schedules
# ---------------------------------------------------------------------------


class SessionEntry(BaseModel):
    """One row in the per-agent sessions list."""

    model_config = ConfigDict(extra="forbid")

    sid: str
    path: str
    size: int
    mtime: float


class SessionsListResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/sessions``."""

    model_config = ConfigDict(extra="forbid")

    sessions: list[SessionEntry]


class SessionReplayResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/sessions/{sid}``."""

    model_config = ConfigDict(extra="forbid")

    sid: str
    page: int
    page_size: int
    total: int
    messages: list[dict[str, Any]]


class TasksResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/tasks`` and ``/team/tasks``."""

    model_config = ConfigDict(extra="forbid")

    tasks: list[dict[str, Any]]


class SchedulesResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/schedules`` (and the team variant)."""

    model_config = ConfigDict(extra="forbid")

    schedules: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Agent detail — telemetry / audit / traces
# ---------------------------------------------------------------------------


class TracesResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/traces`` and ``/api/traces``.

    ``cursor`` is None when there are no more pages. Same model
    handles the empty-store case (traces=[], cursor=None).
    """

    model_config = ConfigDict(extra="forbid")

    traces: list[dict[str, Any]]
    cursor: str | None = None


class StatsResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/stats``.

    ``stats`` is the aggregator's window stats (passthrough dict).
    ``window`` echoes back the requested or default window.
    """

    model_config = ConfigDict(extra="forbid")

    stats: dict[str, Any]
    window: str


class AuditEventsResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/audit`` and ``/api/team/audit``."""

    model_config = ConfigDict(extra="forbid")

    events: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Agent detail — skills / tools
# ---------------------------------------------------------------------------


class SkillsResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/skills``."""

    model_config = ConfigDict(extra="forbid")

    skills: list[dict[str, Any]]


class ToolsResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/tools``."""

    model_config = ConfigDict(extra="forbid")

    tools: list[dict[str, Any]]
    allowlist: list[str]
    denylist: list[str]


# ---------------------------------------------------------------------------
# Agent detail — skill evals + version timeline (SPEC-054 COMP-010)
# ---------------------------------------------------------------------------


class SkillEvalCase(BaseModel):
    """One golden eval case: pytest nodeid + machine/human provenance."""

    model_config = ConfigDict(extra="forbid")

    nodeid: str
    provenance: str  # "machine" | "human"


class SkillEvalCasesResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/skills/{skill_name}/evals``."""

    model_config = ConfigDict(extra="forbid")

    items: list[SkillEvalCase]


class SkillVersionsResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/skills/{skill_name}/versions``.

    Metadata only — candidate bodies never ride the list payload; a
    ``body_hash`` of ``None`` marks a pending/pruned body (``tombstone``).
    """

    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, Any]]


class SkillVersionBodyResponse(BaseModel):
    """Body of ``GET .../skills/{skill_name}/versions/{candidate_id}/body``."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    body: str


class SkillVersionDiffResponse(BaseModel):
    """Body of ``GET .../skills/{skill_name}/versions/diff?a=&b=``."""

    model_config = ConfigDict(extra="forbid")

    a: str
    b: str
    diff: str


class SkillDetailResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/skills/{skill_name}/detail`` (U5).

    ``content`` is the SKILL.md body. ``write_root``/``write_path`` are the
    save target for the existing ``PUT /files/read`` route — both None when
    ``editable`` is False (builtins/global sources never expose a write target).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str
    source_root: str
    source_path: str
    status: str
    status_detail: str
    content: str
    editable: bool
    write_root: str | None
    write_path: str | None


class ToolDetailResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/tools/{tool_name}/detail`` (U6).

    ``content`` is the tool's ``.py`` source. ``write_root``/``write_path``
    mirror :class:`SkillDetailResponse` — set only for agent/workspace-authored
    tool files; builtins and module tools are always read-only.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    transport: str
    classification: str
    description: str
    source_path: str
    content: str
    editable: bool
    write_root: str | None
    write_path: str | None


class SkillRollbackResponse(BaseModel):
    """Body of ``POST .../skills/{skill_name}/rollback``.

    ``warning`` reminds the operator that the target's stored scores are
    historical — they were measured when the candidate was produced and are
    not re-validated by the flip.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    skill_name: str
    from_candidate_id: str | None
    to_candidate_id: str
    warning: str


# ---------------------------------------------------------------------------
# Agent detail — policy
# ---------------------------------------------------------------------------


class PolicyResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/policy``."""

    model_config = ConfigDict(extra="forbid")

    raw: str
    bullets: list[dict[str, Any]]


class PolicyBulletsResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/policy/bullets``."""

    model_config = ConfigDict(extra="forbid")

    bullets: list[dict[str, Any]]


class PolicyStatsResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/policy/stats``."""

    model_config = ConfigDict(extra="forbid")

    total: int
    active: int
    retired: int
    avg_score: float


# ---------------------------------------------------------------------------
# Agent detail — config
# ---------------------------------------------------------------------------


class ConfigResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/config``.

    ``config`` is the whitelisted top-level sections (dict passthrough).
    ``raw`` is the on-disk TOML bytes as a string.
    """

    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any]
    raw: str
    mtime: float


# ---------------------------------------------------------------------------
# Team aggregation routes
# ---------------------------------------------------------------------------


class TeamPolicyStatsResponse(BaseModel):
    """Body of ``GET /api/team/policy/stats``.

    Extends per-agent policy stats (total/active/retired/avg_score) with
    a per-agent breakdown list.
    """

    model_config = ConfigDict(extra="forbid")

    total: int
    active: int
    retired: int
    avg_score: float
    per_agent: list[dict[str, Any]]


class TeamToolsSkillsResponse(BaseModel):
    """Body of ``GET /api/team/tools-skills`` — fleet skills + tools matrix."""

    model_config = ConfigDict(extra="forbid")

    skills: list[dict[str, Any]]
    tools: list[dict[str, Any]]


class AgentsListResponse(BaseModel):
    """Body of ``GET /api/agents`` — fleet listing."""

    model_config = ConfigDict(extra="forbid")

    agents: list[dict[str, Any]]


class ExportTracesResponse(BaseModel):
    """Body of ``GET /api/export?format=json`` — JSON export of traces."""

    model_config = ConfigDict(extra="forbid")

    traces: list[dict[str, Any]]
    count: int
