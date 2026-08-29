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


# ---------------------------------------------------------------------------
# Agent detail — prompts (COMP-010: editable system prompts)
# ---------------------------------------------------------------------------


class PromptListItem(BaseModel):
    """One row in the prompts list — a stock prompt, marked stock or overridden."""

    model_config = ConfigDict(extra="forbid")

    package: str
    name: str
    description: str
    status: str  # "stock" | "overridden"


class PromptListResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/prompts`` — every prompt across packages."""

    model_config = ConfigDict(extra="forbid")

    items: list[PromptListItem]


class PromptDetailResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/prompts/{package}/{name}``.

    ``stock`` is the packaged body; ``effective`` is the overlay body when one is
    present and parseable, else stock; ``diff`` is a server-computed unified diff
    (stock → effective) so the browser ships no diff library.
    """

    model_config = ConfigDict(extra="forbid")

    package: str
    name: str
    description: str
    status: str  # "stock" | "overridden"
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

    ``status`` is ``stock`` or ``overridden`` (same convention as the prompt
    detail); ``dimensions`` is the server-parsed YAML body as structured JSON.
    """

    model_config = ConfigDict(extra="forbid")

    package: str
    name: str
    status: str  # "stock" | "overridden"
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
    """Body of ``GET /api/agents/{id}/tasks`` and ``/team/tasks``."""

    model_config = ConfigDict(extra="forbid")

    tasks: list[dict[str, Any]]


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


class ProviderKeysResponse(BaseModel):
    """Body of ``GET /api/keys``."""

    model_config = ConfigDict(extra="forbid")

    keys: list[ProviderKeyStatus]


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


class ConnectorInstance(BaseModel):
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


class ConnectionsResponse(BaseModel):
    """Body of ``GET /api/connections`` — every connection, and who holds it."""

    model_config = ConfigDict(extra="forbid")

    connections: list[ConnectorInstance]
    extensions_roots: list[str]


class AgentConnectorsResponse(BaseModel):
    """Body of ``GET /api/agents/{id}/connectors`` — what this agent can reach."""

    model_config = ConfigDict(extra="forbid")

    instances: list[ConnectorInstance]
    extensions_roots: list[str]


class ConnectorInstallResponse(BaseModel):
    """Body of ``POST /api/connections`` — what the install produced, and who got it."""

    model_config = ConfigDict(extra="forbid")

    instance: str
    extension: str
    tools: list[str]
    detail: str
    agents: list[str]


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

    sign_in: Literal["signed_in", "signed_out", "unknown"]
    reachable: bool
    detail: str
    command: str


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
    #: True when this connector is finished by an OAuth code exchange — the panel
    #: shows the authorize URL and a code field, not a token form or a host command.
    oauth: bool = False
    #: The provider consent URL to open (empty until the app key/secret are supplied,
    #: or for a non-OAuth connector). Safe to render: it names only the public client
    #: id, never a secret.
    authorize_url: str = ""


class ConnectorProbeResponse(BaseModel):
    """Body of ``POST /api/connections/{instance}/probe``."""

    model_config = ConfigDict(extra="forbid")

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
