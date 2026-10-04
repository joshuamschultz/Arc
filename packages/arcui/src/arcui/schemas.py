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

from typing import Any, Literal

from arcstore.tasks import Task, TaskBoardFacets, TaskBoardProjection
from pydantic import BaseModel, ConfigDict, Field

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


class ConnectedDataActivationResponse(BaseModel):
    """Body of the operator-only connected-data module activation route."""

    model_config = ConfigDict(extra="forbid")

    status: str
    detail: str


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
    mime: str


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


class FileDeleteResponse(BaseModel):
    """Body of ``DELETE /api/agents/{id}/files/read`` (H-018).

    ``protected`` echoes the ADR-029 agent-state tier the deleted path fell
    into: ``"confirm"`` when the operator had to pass ``confirm_protected=true``
    (memory/sessions/context.md), else ``None`` for ordinary content. Blocked
    paths (identity.md, policy.md, the per-agent config TOMLs, signed prompt
    overlays, key material, the audit chain) never reach a 200 — they are
    refused with a 403 before anything is deleted.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    protected: str | None
    message: str


# ---------------------------------------------------------------------------
# Agent detail — prompts (COMP-010: editable system prompts)
# ---------------------------------------------------------------------------


class PromptListItem(BaseModel):
    """One row in the prompts list — a stock prompt, marked stock, overridden or rejected.

    ``rejected`` means an override is present but the agent refuses it (missing,
    unreadable, edited-after-signing or foreign-key signature) and will not run;
    ``rejection_reason`` says which.
    """

    model_config = ConfigDict(extra="forbid")

    package: str
    name: str
    description: str
    status: Literal["stock", "overridden", "rejected"]
    rejection_reason: str | None = None


class PromptListResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/prompts`` — every prompt across packages."""

    model_config = ConfigDict(extra="forbid")

    items: list[PromptListItem]


class RejectedPromptItem(BaseModel):
    """One prompt or signed document the agent refuses to run with."""

    model_config = ConfigDict(extra="forbid")

    package: str
    name: str
    reason: str


class PromptHealthResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/prompts/health`` — empty when the agent can run."""

    model_config = ConfigDict(extra="forbid")

    rejected: list[RejectedPromptItem]


class PromptDetailResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/prompts/{package}/{name}``.

    ``stock`` is the packaged body; ``effective`` is what the agent sends to the
    model — the verified override, or stock when there is none. A ``rejected``
    override has NO effective body (``""``): the agent refuses to run rather than
    fall back to stock, so its text is never shown as the prompt in use. ``diff`` is
    a server-computed unified diff (stock → effective) so the browser ships no diff
    library; empty when rejected.
    """

    model_config = ConfigDict(extra="forbid")

    package: str
    name: str
    description: str
    status: Literal["stock", "overridden", "rejected"]
    rejection_reason: str | None = None
    stock: str
    effective: str
    diff: str


class PromptWriteResponse(BaseModel):
    """Body of ``PUT /api/agents/{id}/prompts/{package}/{name}`` — a signed override.

    ``signer_did`` is the RESOLVED principal that signed the overlay (never a
    constant); ``sha256`` is the signed content digest recorded in the sidecar.
    """

    model_config = ConfigDict(extra="forbid")

    package: str
    name: str
    signer_did: str
    sha256: str
    message: str


class PromptVersionItem(BaseModel):
    """One signed, immutable stored version of a prompt (J2 F3)."""

    model_config = ConfigDict(extra="forbid")

    version: int
    sha256: str
    signer_did: str
    signed_at: str | None = None
    #: True when this version's bytes are what is live on disk right now.
    current: bool


class PromptHistoryResponse(BaseModel):
    """Body of ``GET .../prompts/{package}/{name}/history`` — newest version first."""

    model_config = ConfigDict(extra="forbid")

    package: str
    name: str
    versions: list[PromptVersionItem]


class PromptHistoryDiffResponse(BaseModel):
    """Body of ``GET .../history/diff`` — a unified diff between two versions."""

    model_config = ConfigDict(extra="forbid")

    package: str
    name: str
    from_label: str
    to_label: str
    diff: str


class PromptRevertResponse(BaseModel):
    """Body of ``POST .../history/{version}/revert`` — the NEW signed version it created."""

    model_config = ConfigDict(extra="forbid")

    package: str
    name: str
    reverted_from: int
    new_version: int
    signer_did: str
    sha256: str
    message: str


class SemanticLayerResponse(BaseModel):
    """Body of ``GET /api/connections/{instance}/semantic-layer`` (H-025).

    ``content`` is the raw TOML — this file is hand-edited, so the browser
    round-trips exact text rather than a reconstructed model. ``signed`` is
    whether a verified ``.arcsig`` sidecar is currently backing it.
    """

    model_config = ConfigDict(extra="forbid")

    connection_id: str
    exists: bool
    content: str
    classification: str
    signed: bool


class SemanticLayerWriteResponse(BaseModel):
    """Body of ``PUT /api/connections/{instance}/semantic-layer`` — a signed save."""

    model_config = ConfigDict(extra="forbid")

    connection_id: str
    signer_did: str
    sha256: str
    message: str


class PromptResetResponse(BaseModel):
    """Body of ``DELETE /api/agents/{id}/prompts/{package}/{name}`` — override removed."""

    model_config = ConfigDict(extra="forbid")

    package: str
    name: str
    message: str


class RubricDimension(BaseModel):
    """One judge-rubric dimension: an ordered checklist plus a calibration note.

    ``checklist`` rows and ``anti_inflation`` mirror the YAML body of the
    ``arcskill/judge_rubric`` prompt exactly; this model is the structured editing
    surface over that same signed overlay.
    """

    model_config = ConfigDict(extra="forbid")

    checklist: list[str]
    anti_inflation: str


class RubricUpdate(BaseModel):
    """Request body of ``PUT .../prompts/{package}/{name}/rubric`` — a structured edit.

    ``dimensions`` is a mapping of dimension name → :class:`RubricDimension`;
    insertion order is preserved on serialization back to YAML (``sort_keys=False``).
    """

    model_config = ConfigDict(extra="forbid")

    dimensions: dict[str, RubricDimension]


class RubricResponse(BaseModel):
    """Body of ``GET .../prompts/{package}/{name}/rubric`` — the parsed rubric.

    ``status`` follows the prompt-detail convention (``stock`` / ``overridden`` /
    ``rejected``); ``dimensions`` is the server-parsed YAML body as structured JSON —
    the stock rubric when the override is rejected, so the operator can re-author it.
    """

    model_config = ConfigDict(extra="forbid")

    package: str
    name: str
    status: Literal["stock", "overridden", "rejected"]
    rejection_reason: str | None = None
    dimensions: dict[str, RubricDimension]


# ---------------------------------------------------------------------------
# Agent detail — sessions / tasks / schedules
# ---------------------------------------------------------------------------


class SessionEntry(BaseModel):
    """One row in the per-agent sessions list.

    The trailing fields enrich the Inbox tab: ``kind`` classifies the session
    (``messaging`` teammate DM, ``chat`` human, or the sid namespace for
    ``cli``/``pulse``/``scheduler``/…); ``counterpart`` is the teammate's display
    name when the sid resolves to a known peer (``None`` otherwise — a human
    correspondent's DID is not recoverable from the one-way session key);
    ``message_count``/``last_role``/``last_text``/``last_ts`` come from a bounded
    read of the session transcript (all ``None`` when the file is unreadable or
    too large).
    """

    model_config = ConfigDict(extra="forbid")

    sid: str
    path: str
    size: int
    mtime: float
    kind: str = "chat"
    counterpart: str | None = None
    message_count: int | None = None
    last_role: str | None = None
    last_text: str | None = None
    last_ts: str | None = None
    # True for the caller's *current* (rotation-aware) conversation — the one a
    # new message lands in and a refresh must resolve. After a ``/new`` this is
    # the rotated generation, not the stale generation-0 base key.
    current: bool = False


class SessionsListResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/sessions``."""

    model_config = ConfigDict(extra="forbid")

    sessions: list[SessionEntry]
    # The caller's current session key (``SessionRouter.current_session_key``),
    # so a client loads/highlights the live conversation by default instead of
    # re-deriving the base key, which diverges from the write path after a
    # rotation. ``None`` when no SessionRouter is wired (read-only deployments).
    current_session_key: str | None = None


class SessionReplayResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/sessions/{sid}``."""

    model_config = ConfigDict(extra="forbid")

    sid: str
    page: int
    page_size: int
    total: int
    messages: list[dict[str, Any]]


class TasksResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/tasks``."""

    model_config = ConfigDict(extra="forbid")

    tasks: list[dict[str, Any]]


class TaskBoardItem(Task):
    model_config = ConfigDict(frozen=True, extra="forbid")
    blocked_by_total: int
    tags_total: int
    agent_id: str | None = None


class TaskBoardResponse(BaseModel):
    """Validated fleet-board wire contract, separate from agent task lists."""

    model_config = ConfigDict(extra="forbid")
    tasks: list[TaskBoardItem]
    next_cursor: str | None = None
    facets: TaskBoardFacets
    projections: dict[str, TaskBoardProjection]


class HomeNeedsQueue(BaseModel):
    """One operator-action queue inside ``HomeNeedsResponse``: a true count
    plus a short, capped preview list an operator can act on without leaving
    Home."""

    model_config = ConfigDict(extra="forbid")

    count: int
    items: list[dict[str, Any]]


class HomeNeedsResponse(BaseModel):
    """Body of ``GET /api/home/needs`` — Home's aggregated "NEEDS YOU" panel
    (H-001). ``total`` is the sum of every queue's ``count`` — never derived
    from the (possibly truncated) ``items`` lists, so "all caught up" means
    every queue really is empty, not just that none of them fit the preview.

    ``waiting_on_human`` (H-001b) is the fourth queue: runs blocked because an
    agent asked the operator a question over a channel and no human has replied.
    It counts channel questions ONLY — a run paused on an approval or a workflow
    gate is already in ``approvals`` / ``review_tasks`` and is excluded here by
    structural provenance, so it is never double-counted.

    ``pulse`` and ``schedules`` carry the pulse checks and legacy schedules that
    cannot run until the operator approves them. Each row holds what the owning
    subsystem's own approve route needs, so the inbox adds no second approval path.
    """

    model_config = ConfigDict(extra="forbid")

    approvals: HomeNeedsQueue
    capabilities: HomeNeedsQueue
    review_tasks: HomeNeedsQueue
    waiting_on_human: HomeNeedsQueue
    pulse: HomeNeedsQueue
    schedules: HomeNeedsQueue
    total: int


class SchedulesResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/schedules`` (and the team variant)."""

    model_config = ConfigDict(extra="forbid")

    schedules: list[dict[str, Any]]


class ChannelsResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/channels`` — delivery-target picker source.

    Each item is ``{"target": "platform:chat_id", "label": "Telegram — Josh"}``,
    newest first, so arcui can offer a schedule's delivery channel as a dropdown
    instead of a raw ``platform:chat_id`` box.
    """

    model_config = ConfigDict(extra="forbid")

    channels: list[dict[str, str]]


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


class FleetAuditResponse(BaseModel):
    """Body of ``GET /api/team/audit`` — a ledger page plus the whole ledger's counts."""

    model_config = ConfigDict(extra="forbid")

    events: list[dict[str, Any]]
    totals: dict[str, int]
    """``{total, verified, broken}`` over every mirrored record, not the page (item 20)."""


# ---------------------------------------------------------------------------
# Agent detail — skills / tools
# ---------------------------------------------------------------------------


class SkillsResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/skills``."""

    model_config = ConfigDict(extra="forbid")

    skills: list[dict[str, Any]]


class ToolsResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/tools``.

    ``policy_summary`` (H-010) is the ONE authoritative policy verdict —
    ``arcagent.ToolPolicySummary`` serialized — that both the Tools tab and
    the Identity tab render their headline label from, so the two surfaces
    can no longer disagree about what an empty allowlist means.

    ``tools`` (H-013/H-014) is the single consolidated tool list — durable
    scan fields (``name``, ``transport``, ``classification``, ``description``,
    ``status`` allow/deny) merged with the capability loader's verbatim
    verdict (``version``, ``source_root``, ``loader_status``,
    ``loader_detail`` — the signature/TOFU provenance) and a normalized
    ``source`` badge category (``builtin`` / ``agent`` / ``extension`` /
    ``module``). One row, one tool — no second "loader verdicts" table.
    """

    model_config = ConfigDict(extra="forbid")

    tools: list[dict[str, Any]]
    allowlist: list[str]
    denylist: list[str]
    policy_summary: dict[str, Any]


# ---------------------------------------------------------------------------
# Agent detail — skill evals + version timeline (SPEC-054 COMP-010)
# ---------------------------------------------------------------------------


class SkillEvalCase(BaseModel):
    """One golden eval case: pytest nodeid + provenance + gate type (H-041)."""

    model_config = ConfigDict(extra="forbid")

    nodeid: str
    provenance: str  # "machine" | "human" | "curated"
    gate_type: str = "exact_match"  # exact_match | assertions | judge_rubric


class SkillEvalCasesResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/skills/{skill_name}/evals``."""

    model_config = ConfigDict(extra="forbid")

    items: list[SkillEvalCase]


class SkillPromoteGoldenResponse(BaseModel):
    """Body of ``POST /api/agents/{id}/skills/{skill_name}/promote`` (H-041)."""

    model_config = ConfigDict(extra="forbid")

    status: str  # "emitted"
    skill_name: str
    nodeid: str
    gate_type: str


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
    sha256: str
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


class AgentConfigFileResponse(BaseModel):
    """Body of ``GET/PATCH /api/agents/{id}/config/{file}``.

    ``sections`` is the file's top-level TOML tables (dict passthrough), the
    editable surface of the per-agent config editor. ``mtime`` is ``0.0`` when
    the file does not exist (the editor renders an empty state).
    """

    model_config = ConfigDict(extra="forbid")

    file: str
    sections: dict[str, Any]
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


# ---------------------------------------------------------------------------
# SPEC-064 — provider keys and connectors
# ---------------------------------------------------------------------------
#
# These models carry no field able to hold a credential value, and that is the
# point rather than an accident of the shapes chosen (D-583, D-585). A key or a
# connector secret reaching a browser would be a leak in the one surface that
# cannot un-send it, so ``extra="forbid"`` plus the absence of a value field
# means a route trying to include one raises here instead of serializing it.


class ProviderKeyStatus(BaseModel):
    """One row of ``GET /api/keys`` — a coordinate and whether a value is stored."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    env_var: str
    required: bool
    present: bool
    # ``model`` = an AI provider, ``web`` = a web search / extract service.
    kind: Literal["model", "web"] = "model"


class ProviderKeysResponse(BaseModel):
    """Body of ``GET /api/keys``."""

    model_config = ConfigDict(extra="forbid")

    keys: list[ProviderKeyStatus]


class ClassifierModelsResponse(BaseModel):
    """Body of ``GET /api/classifiers/{name}/models``."""

    model_config = ConfigDict(extra="forbid")

    classifier: str
    models: list[str]


class ProviderKeySetResponse(BaseModel):
    """Body of ``PUT /api/keys/{env_var}`` — the value is never echoed."""

    model_config = ConfigDict(extra="forbid")

    env_var: str
    present: bool


class ProviderKeyDeleteResponse(BaseModel):
    """Body of ``DELETE /api/keys/{env_var}``.

    ``removed`` is False when there was nothing to forget, which is a report and
    never an error.
    """

    model_config = ConfigDict(extra="forbid")

    env_var: str
    present: bool
    removed: bool


class ConnectorSecretField(BaseModel):
    """One value the operator must supply: its field name, its prompt, and its kind.

    ``sensitive`` tells the form whether to mask the input. Not everything a bundle
    asks for is a credential — a base URL and an account address are configuration —
    and masking those bought no protection while hiding the one thing an operator
    needed to check, on a form where a mistyped URL fails at probe with no clue why.

    ``value`` is populated ONLY for a non-sensitive field, and only by the verb that
    reports one connected instance. A sensitive value is never read out of the store,
    so this field cannot carry one whatever a caller does.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    prompt: str
    sensitive: bool = True
    value: str = ""
    #: The manifest's shape for the field, so a form can leave an optional one
    #: blank, draw a choice as a choice, and say what blank means.
    required: bool = True
    choices: list[str] = Field(default_factory=list)
    default: str = ""
    #: Catalog: the bundle's warning for leaving this field blank. A connected
    #: instance: the same text, present only while the stored value IS blank.
    warning: str = ""
    #: True for the OAuth refresh token: Connect fills it in, so a form never asks a person
    #: to type it.
    managed: bool = False


class ConnectorHostRequirement(BaseModel):
    """A host prerequisite, and whether THIS host already meets it.

    ``satisfied`` is the difference between a catalog that describes a manifest and
    one that describes a deployment. Without it a card shows the full install
    instructions for a binary that has been on the machine for hours, which reads as
    "this is broken" to the operator it is aimed at.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    instruction: str
    satisfied: bool = False


class ConnectorTool(BaseModel):
    """One tool a bundle declares or a live connection serves."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    classification: str
    capability_tags: list[str]


class ConnectorCatalogEntry(BaseModel):
    """One installable bundle on the extension search path."""

    model_config = ConfigDict(extra="forbid")

    name: str
    #: The product's own name, for a person to read. ``name`` stays the coordinate
    #: every request is keyed by; this is only ever rendered.
    display_name: str
    version: str
    description: str
    knowledge_mode: str
    knowledge_reason: str
    attachment: str
    tier_floor: str
    approval_default: str
    secrets: list[ConnectorSecretField]
    host_requires: list[ConnectorHostRequirement]
    tools: list[ConnectorTool]
    root: str
    #: True when Arc can place this bundle's host program itself on this machine. False
    #: means an Install button could never succeed, so the page hides it.
    auto_installable: bool = False
    #: The deployment sign-in app a one-click connect uses ("" when the bundle is not OAuth).
    #: Non-empty means: after adding it, go straight into Connect.
    oauth_provider: str = ""


class ConnectorUnreadableBundle(BaseModel):
    """A directory that looked like a bundle and could not be read, and why.

    Listed rather than dropped: a bundle that vanishes silently is a support
    call, and one that 500s the listing takes every other bundle with it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    reason: str


class ConnectorCatalogResponse(BaseModel):
    """Body of ``GET /api/connectors/catalog``."""

    model_config = ConfigDict(extra="forbid")

    available: list[ConnectorCatalogEntry]
    unreadable: list[ConnectorUnreadableBundle]


ConnectionStatusName = Literal["unknown", "healthy", "needs_you", "error"]
ConnectionDisplayStatus = Literal["unknown", "healthy", "needs_you", "error", "syncing"]
ConnectionActionName = Literal["none", "reconnect", "approve", "install_host", "wait"]
ConnectKind = Literal["oauth", "token", "host_login", "none"]


class LastNoticeView(BaseModel):
    """The last operator notice a connection earned, delivered or given up on."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["needs_you", "error", "recovered"]
    delivered: bool
    channel: str
    at: str


class KnowledgeSyncRow(BaseModel):
    """One agent's connected-knowledge sync of a connection, read from the durable row.

    ``running`` is true only while the row's lease is live: a crashed run leaves
    ``state == "running"`` behind forever and must not read as syncing.
    """

    model_config = ConfigDict(extra="forbid")

    agent: str
    source_id: str
    state: str
    running: bool
    last_synced_at: str | None = None
    pages: int = 0
    error_code: str | None = None


class ConnectionHealthView(BaseModel):
    """The one truthful health answer for a connection (the card's chip and button).

    Everything here is read from the connection's health record and the durable sync
    rows. Nothing is probed and no credential is read to produce it, so a page view
    costs no provider call and writes no ``secret.read`` row.
    """

    model_config = ConfigDict(extra="forbid")

    status: ConnectionStatusName = "unknown"
    display_status: ConnectionDisplayStatus = "unknown"
    reason_code: str | None = None
    reason_text: str | None = None
    action: ConnectionActionName = "none"
    action_label: str = ""
    last_checked_at: str | None = None
    last_success_at: str | None = None
    last_notice: LastNoticeView | None = None
    #: True for a one-click connection whose deployment sign-in app is not set up yet:
    #: nothing can connect until it is, so the card's action opens that form first.
    app_missing: bool = False


class ConnectorInstance(ConnectionHealthView):
    """One connected account, as a listing row — including who may use it.

    ``agents`` is the grant list and the only thing that decides access, so it
    travels on every row. A row without it renders a connected account as
    available to everyone, which is the opposite of what deny-by-default means:
    an operator reading such a row has no way to see that their agent holds
    nothing.
    """

    model_config = ConfigDict(extra="forbid")

    instance: str
    extension: str
    #: What to call ``extension`` in front of a person. Falls back to the
    #: coordinate for a bundle that declares none, never blank.
    extension_display_name: str
    knowledge_mode: str = ""
    knowledge_reason: str = ""
    approval: str
    agents: list[str]
    #: How the operator reconnects it, read from the manifest alone.
    connect_kind: ConnectKind = "none"
    #: The deployment app slot a one-click connect uses ("" when not OAuth).
    oauth_provider: str = ""
    knowledge_sync: list[KnowledgeSyncRow] = Field(default_factory=list)


class ConnectionsResponse(BaseModel):
    """Body of ``GET /api/connections`` — every connection, and who holds it."""

    model_config = ConfigDict(extra="forbid")

    connections: list[ConnectorInstance]
    extensions_roots: list[str]


class AgentConnectorInstance(ConnectorInstance):
    """One connected account on the per-agent panel, plus whether it waits on a person.

    ``needs_attention`` is the connection's own health record saying ``needs_you``:
    a revoked or expired credential no retry can clear. It is the same fact the card's
    chip shows (the row also carries the full health fields), so the two panels cannot
    disagree. A connection with no record reads as not needing attention: "unknown" is
    never rendered as an error.
    """

    needs_attention: bool = False


class AgentConnectorsResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/connectors`` — what this agent can reach.

    ``mcp_door_enabled`` (SPEC-082 COMP-008) reports whether this agent's MCP
    server door is open — i.e. ``[modules.mcp_server]`` is enabled in the agent
    config. The door is DEFAULT OFF and this field fails closed to ``False``
    whenever the agent or its config cannot be read.
    """

    model_config = ConfigDict(extra="forbid")

    instances: list[AgentConnectorInstance]
    extensions_roots: list[str]
    mcp_door_enabled: bool = False


class ConnectorInstallResponse(BaseModel):
    """Body of ``POST /api/connections`` — what the install produced, and who got it."""

    model_config = ConfigDict(extra="forbid")

    instance: str
    extension: str
    tools: list[str]
    detail: str
    agents: list[str]


class McpToolView(BaseModel):
    """One tool an MCP server advertised. The description is the server's own, untrusted text."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    usable: bool
    reason: str = ""


class McpPreviewResponse(BaseModel):
    """Body of ``POST /api/mcp-servers/preview`` — what the server offers, nothing written."""

    model_config = ConfigDict(extra="forbid")

    tools: list[McpToolView]
    suggested_tags: list[str]


class McpServerAddedResponse(BaseModel):
    """Body of ``POST /api/mcp-servers`` — names and a digest, never a credential."""

    model_config = ConfigDict(extra="forbid")

    instance: str
    extension: str
    tools: list[str]
    detail: str
    agents: list[str]
    spec_sha256: str


class ConnectorHostBlockedResponse(BaseModel):
    """400 body when the host lacks a prerequisite: the error plus what to install."""

    model_config = ConfigDict(extra="forbid")

    error: str
    unsatisfied_host: list[ConnectorHostRequirement]


class ConnectorAuthResponse(BaseModel):
    """Body of ``PUT /api/connections/{instance}/auth`` — field names only."""

    model_config = ConfigDict(extra="forbid")

    instance: str
    updated: list[str]


class ConnectorHostAuthorization(BaseModel):
    """One host binary that holds its own credential, and the command that grants it.

    ``token_command`` non-empty means Arc can complete this sign-in itself, given
    a token; empty means only a person at the host can finish it.
    """

    model_config = ConfigDict(extra="forbid")

    binary: str
    command: str
    instruction: str
    token_command: str


class ConnectorAuthStatusResponse(BaseModel):
    """Body of the ``auth-status`` and ``authorize`` verbs — the sign-in, honestly.

    ``sign_in`` and ``reachable`` are two questions with two answers and the panel
    needs both. ``reachable`` is "does this connection answer at all";
    ``sign_in`` is "is this account connected". They were one field, taken from
    the probe, and a ``dbxcli`` that had never been signed in reported
    **Signed in — dbxcli version: 3.7.1** because ``dbxcli version`` runs
    perfectly well with no credential.

    ``unknown`` is a real value, not a placeholder: a bundle declaring no way to
    check must not be drawn as a green tick, and must not be drawn as a failure
    either — that would send an operator to redo a login already done.

    ``detail`` is the evidence for ``sign_in`` and nothing else: the command that
    ran, and what it answered. Empty means nothing was checked. It used to be the
    probe's line, which put "signs in on this host; Arc ran nothing" inside a
    green "Signed in" box — a badge citing a check nobody had performed.

    ``command`` is present ONLY when Arc cannot finish the sign-in itself: the
    panel renders it under "someone with terminal access can type this", so
    returning one for a login the button would have completed sends the operator
    away from the thing that works. It has no field able to hold a credential.
    """

    model_config = ConfigDict(extra="forbid")

    sign_in: Literal["signed_in", "signed_out", "expired", "not_installed", "unknown"]
    reachable: bool
    detail: str
    command: str


class OAuthBeginResponse(BaseModel):
    """Body of ``POST /api/connections/{instance}/oauth/begin``.

    ``authorize_url`` carries only public values (client id, ``state``, a PKCE
    challenge, the configured redirect URI); the verifier stays on the server.
    """

    model_config = ConfigDict(extra="forbid")

    authorize_url: str
    state: str
    redirect_mode: Literal["callback", "none"]
    expires_in: int


class OAuthCloudChoice(BaseModel):
    """One sovereign cloud an app slot may name: its key and what a person calls it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str


class OAuthAppResponse(BaseModel):
    """Body of ``GET /api/oauth-apps/{provider}``. Never the client secret."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    configured: bool
    client_id_hint: str
    redirect_uri: str
    console_url: str
    #: The provider's app slot needs a directory (tenant) id (Microsoft Entra ID).
    tenant_required: bool = False
    #: The stored tenant id and cloud key: not secrets, shown so they can be checked.
    tenant_id: str = ""
    cloud: str = ""
    #: The clouds the bundle declares, as ``{id, label}``, default first; empty when none.
    clouds: list[OAuthCloudChoice] = Field(default_factory=list)


class ConnectorHostSetupResponse(BaseModel):
    """Body of ``POST /api/connections/{extension}/host-setup``.

    A refusal is a 200 with ``installed: false``: the reason and the steps a
    person runs instead are both operator-facing text, and an operator left with
    an error and no next step is the wall this button exists to remove.
    """

    model_config = ConfigDict(extra="forbid")

    installed: bool
    detail: str
    manual_steps: str
    #: What the page should offer next, as a code rather than prose naming a command.
    #: ``""`` asks nothing; ``restart_arc`` means the program is placed but Arc must restart.
    action: str = ""


class ConnectorAuthorizationResponse(BaseModel):
    """Body of ``GET /api/connections/{instance}/auth`` — how to connect this.

    ``credentials`` non-empty means render the form; ``hosts`` non-empty means
    render the command the operator runs on the machine instead. A connector whose
    binary owns its token has an empty ``credentials`` list, which is why a panel
    that only read that list drew an empty form over a dead button.
    """

    model_config = ConfigDict(extra="forbid")

    instance: str
    extension: str
    #: The heading the connect panel shows. Same rule as everywhere else: a name
    #: for a person, never a substitute for the coordinate beside it.
    extension_display_name: str
    credentials: list[ConnectorSecretField]
    hosts: list[ConnectorHostAuthorization]
    reachable: bool
    detail: str
    #: True when this connector connects with one click (native OAuth): the card
    #: runs the begin/complete flow, never a token form or a host command.
    oauth: bool = False
    #: The account-connected answer, taken with the same check ``auth-status``
    #: runs, so a card reading this one call can show it without a second.
    sign_in: Literal["signed_in", "signed_out", "expired", "not_installed", "unknown"] = "unknown"


class ConnectorProbeResponse(ConnectionHealthView):
    """Body of ``POST /api/connections/{instance}/probe``.

    The health fields are the record the check just wrote; ``reachable``, ``detail``
    and ``tools`` are what the doctor panel reads.
    """

    reachable: bool
    detail: str
    tools: list[ConnectorTool]


class ConnectorDoctorCheck(BaseModel):
    """One diagnostic row — the same rows ``arc connector doctor`` prints."""

    model_config = ConfigDict(extra="forbid")

    check: str
    status: str
    detail: str


class ConnectorDoctorResponse(BaseModel):
    """Body of ``GET /api/connections/{instance}/doctor``."""

    model_config = ConfigDict(extra="forbid")

    checks: list[ConnectorDoctorCheck]


class ConnectorApproveResponse(BaseModel):
    """Body of ``POST /api/connections/{instance}/approve`` (REQ-291)."""

    model_config = ConfigDict(extra="forbid")

    instance: str
    approved: list[str]


class ConnectorRemoveResponse(BaseModel):
    """Body of ``DELETE /api/connections/{instance}`` — what was dropped."""

    model_config = ConfigDict(extra="forbid")

    instance: str
    removed_secrets: list[str]
    removed_config: bool
    removed_state: bool
