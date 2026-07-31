# Solution Design Document: ArcUI Reality Mirror

## Context References

- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Project structure:** [.claude/steering/structure.md](../../steering/structure.md)
- **PRD:** [PRD.md](./PRD.md)

## Overview

Three drifted ArcUI views are replaced by consumers of the runtime's own APIs: a Knowledge view over arcmemory (episodic store + entity graph in index.db), a Channels view wired to the live arcteam MessagingService, and Capability views fed by arcagent's CapabilityLoader inventory including trust verdicts. One new principle-enforcing seam per producer package; arcui contains zero discovery logic. Mutations (memory edit/delete, channel create/membership) are operator-token-gated and audited through a single UI-audit helper. Stack per `.claude/steering/tech.md#tech-stack`; layering per `.claude/steering/structure.md#layer-model` (arcui sits atop arcagent/arcteam/arcmemory; dependencies point strictly down).

## Architecture

Backend: Starlette routes in packages/arcui/src/arcui/routes/* read shared handles from app.state, wired once in server.py's lifespan (embedded pattern, SPEC-023). Three producer seams: (1) arcmemory gains a thin operator facade module (arcmemory.operator) exposing list/search/get/links/edit/delete over MemoryDB + EpisodicStore + SemanticStore + WeightedGraph — the only surface arcui calls (REQ-087); (2) server.py lifespan constructs the arcteam MessagingService (same construction path arccli's _build_service uses, against the bootstrap-managed NATS/team root) and sets app.state.messaging_service — the handle team_chat.py already reads but nothing ever set; the None fallback becomes an explicit error payload instead of fail-open []; (3) arcagent gains a non-core inventory module (arcagent.capabilities.inventory) that runs CapabilityLoader discovery across the four scan roots and returns per-item records with the TofuLayer/loader verdict verbatim. Frontend: React/vite pages consume the new JSON endpoints; bundle rebuilt into src/arcui/static. Auth: existing auth.py roles — viewer=read, operator=mutate. Audit: one ui_audit helper wraps the existing audit seam; every mutation route calls it (single emission point).

## Components

### COMP-001: arcmemory.operator facade
**Responsibility:** Public read/mutation API over an agent's memory database: list episodic memories (paged, with created/score/decay/source metadata), search (Retriever/SurfaceIndex), get entry, list entity links (WeightedGraph), edit entry text/metadata, delete entry. Owns all SQL/store access; returns typed Pydantic records. Also supports metadata adjustment (importance score, decay-relevant fields) on entries.
**Dependencies:** MemoryDB, EpisodicStore, SemanticStore, WeightedGraph, Retriever
**Inputs:** workspace memory dir path; queries (page, search text, entry id); mutations (entry id, new text) with actor DID
**Outputs:** MemoryRecord/EntityRecord/LinkRecord lists incl. metadata; MutationResult (applied | error, never partial)

### COMP-002: arcui knowledge routes
**Responsibility:** REST surface per agent: GET list/search/detail/links; PATCH edit; DELETE remove. Viewer token for reads, operator token for mutations (403 otherwise); every mutation emits a ui audit event; arcmemory errors surfaced verbatim; distinguishes empty store from unreadable store.
**Dependencies:** COMP-001, COMP-010, auth.py roles
**Inputs:** HTTP /api/agents/{agent}/knowledge[...] with bearer token
**Outputs:** JSON records incl. metadata; 403 on role violation; explicit error state payloads

### COMP-003: Knowledge frontend view
**Responsibility:** Browse/search/navigate an agent's memories and entities: metadata columns (created, decay/recency, score 1-10, source), link navigation, edit/delete affordances gated to operator role, empty-vs-error states.
**Dependencies:** COMP-002
**Inputs:** knowledge endpoints JSON
**Outputs:** rendered view; user mutations as PATCH/DELETE calls

### COMP-004: Embedded messaging-service wiring
**Responsibility:** server.py lifespan constructs the arcteam MessagingService against the same team-registry root/NATS the embedded bootstrap already starts, sets app.state.messaging_service, and closes it on shutdown. team_chat routes stop failing open: missing/failed service returns an explicit service-unavailable error payload, never [].
**Dependencies:** arcteam MessagingService, bootstrap_infra-managed NATS, server.py lifespan
**Inputs:** team root + arcteam config at startup
**Outputs:** app.state.messaging_service (live handle) | explicit error payload {error: service_unavailable} from channel routes

### COMP-005: Channel management routes
**Responsibility:** POST /api/team/channels (create; refuse duplicate names — same check as arc team create-channel), POST/DELETE /api/team/channels/{name}/members (membership via the same service calls as arc team update-entity/create-channel path, resolving agent refs through the registry). Operator token required; every mutation audited.
**Dependencies:** COMP-004, COMP-010, arcteam MessagingService/EntityRegistry
**Inputs:** HTTP POST/DELETE with operator bearer + JSON {name, members[]}
**Outputs:** created/updated channel JSON; 409 on duplicate; 403 on role violation

### COMP-006: Channels frontend view
**Responsibility:** List real channels with members; create-channel dialog; add/remove member controls gated to operator role; renders service-unavailable state distinctly from no-channels.
**Dependencies:** COMP-004, COMP-005
**Inputs:** channels endpoints JSON
**Outputs:** rendered channel management UI

### COMP-007: arcagent capability inventory seam
**Responsibility:** Non-core module (arcagent/capabilities/inventory.py) that enumerates skills and capability tools for an agent dir across all four scan roots via CapabilityLoader, capturing each item's loader outcome (loaded | tofu-denied | invalid | future verdicts) verbatim from the TofuLayer/loader result, with name, version, description, source root, path.
**Dependencies:** CapabilityLoader, TofuLayer
**Inputs:** agent directory path
**Outputs:** list[CapabilityInventoryItem] {kind: skill|tool, name, version, description, source_root, status, status_detail}

### COMP-008: arcui capability routes
**Responsibility:** Replace team_pages' stale workspace/skills/*.md glob: per-agent and fleet endpoints return COMP-007 inventory plus the runtime tool registry list for loaded agents (every registered tool, not presets).
**Dependencies:** COMP-007, embedded agent registry
**Inputs:** HTTP GET /api/agents/{agent}/capabilities, /api/team/tools-skills
**Outputs:** JSON inventory incl. source root + status per item

### COMP-009: Capabilities frontend view
**Responsibility:** Skills/tools tables with source-root and trust-status badges (status strings rendered verbatim — no client-side enum), load-error detail popover, covering agent detail and fleet views.
**Dependencies:** COMP-008
**Inputs:** capability endpoints JSON
**Outputs:** rendered capability views

### COMP-010: UI mutation audit helper
**Responsibility:** Single emission point for UI-originated mutations: actor (token role + session), agent/target, operation, outcome — wraps the existing arcui/arcstore audit seam; used by knowledge and channel mutation routes.
**Dependencies:** existing audit sink seam
**Inputs:** audit(actor, target, operation, outcome, detail)
**Outputs:** audit event persisted via existing sink

### COMP-011: Docs and release notes
**Responsibility:** Update docs/deploy/single-node.md + team-building.md (new UI capabilities), README ArcUI section, arcui CHANGELOG and root CHANGELOG.
**Dependencies:** COMP-002, COMP-005, COMP-008
**Inputs:** shipped behavior
**Outputs:** docs describing exactly what shipped

### COMP-012: Agent workspace file editor
**Responsibility:** View + operator-gated edit of agent workspace files (identity.md and other text files) through arcui: extends the existing files/tree read endpoints with a PUT save path enforcing canonical-path workspace confinement (same rule family as agent tool confinement) and the secret-content guard; every save audited; markdown rendered in view mode.
**Dependencies:** existing arcui files/tree routes, COMP-010
**Inputs:** GET/PUT /api/agents/{agent}/files?path=<rel> with bearer token
**Outputs:** file content JSON; saved revision ack; 403 viewer; 400 path-escape/secret-content refusal


## Data Model

No new persistent stores. arcmemory index.db remains the single source for memories/entities/links (schema owned by arcmemory; facade returns typed records). Channel/entity data remains in arcteam's NATS-backed registry. Capability inventory is computed on demand, never persisted. New Pydantic response models in arcui/schemas.py mirror facade/inventory records.

## External Integrations

arcmemory (new operator facade, in-process), arcteam MessagingService + EntityRegistry (in-process over managed NATS), arcagent CapabilityLoader/TofuLayer (in-process). No external network integrations.

## Traceability

| Requirement | Components |
|---|---|
| REQ-084 | COMP-001, COMP-002, COMP-003 |
| REQ-085 | COMP-001, COMP-002, COMP-003 |
| REQ-086 | COMP-001, COMP-002 |
| REQ-087 | COMP-001 |
| REQ-088 | COMP-002, COMP-010 |
| REQ-089 | COMP-001, COMP-002 |
| REQ-090 | COMP-004 |
| REQ-091 | COMP-005, COMP-010 |
| REQ-092 | COMP-005, COMP-010 |
| REQ-093 | COMP-007, COMP-008, COMP-009 |
| REQ-094 | COMP-007, COMP-009 |
| REQ-095 | COMP-008, COMP-009 |
| REQ-096 | COMP-007, COMP-008 |
| REQ-097 | COMP-002, COMP-003 |
| REQ-098 | COMP-011 |
| REQ-099 | COMP-012, COMP-010 |
| REQ-100 | COMP-001, COMP-002, COMP-003 |

## Alternatives Considered

Raw SQL in arcui against index.db (rejected: violates module boundaries and REQ-087; schema drift would silently break the UI — the exact failure mode this feature exists to end). GraphQL layer for knowledge queries (rejected: new dependency + complexity for one consumer; plain REST + typed records suffices). Reusing arc team CLI via subprocess from arcui (rejected: process-spawn per request, loses typed errors; the service API is in-process and already shared with the CLI). Persisting capability inventory in arcstore (rejected: cache invalidation burden; discovery is cheap and truth lives with the loader).

## Risks and Mitigations

arcmemory facade may reveal gaps in store APIs (e.g. no delete on episodic entries) — mitigation: facade task explicitly includes adding minimal store methods inside arcmemory with TDD. Parallel task 16 changes TOFU semantics — mitigation: COMP-007 passes verdict strings through verbatim; no arcui enum. arcgateway core LOC budget nearly full — mitigation: zero changes to arcgateway core; channels wiring lives in arcui server.py lifespan. Frontend bundle rebuild drift — mitigation: rebuild step in PLAN with tsc/eslint gates and bundle committed alongside backend.

## Open Questions

_(none)_
