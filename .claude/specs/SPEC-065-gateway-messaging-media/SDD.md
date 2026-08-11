# Solution Design Document: Gateway Messaging + Media

## Context References

- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Project structure:** [.claude/steering/structure.md](../../steering/structure.md)
- **PRD:** [PRD.md](./PRD.md)

## Overview

One inbound path and one outbound path for every surface. A platform adapter becomes a thin translator between its wire format and a shared part vocabulary; everything security-relevant — download, naming, size ceilings, audit, session identity, pairing, splitting — moves into the gateway where it is written once. Media is written to the agent workspace and referenced, never carried as bytes through the queue, the session log, or the prompt. Delivery stops being a gateway decision and becomes an agent one, which is where it already was for teammates.

## Architecture

Layering follows `.claude/steering/structure.md#dependency-direction` and the root CLAUDE.md graph: arcllm <- arcrun <- arcagent <- {arcgateway, arcui}. arcgateway funnels messages into arcagent and never imports arcllm; arcagent reaches model types through the arcrun facade only (`import arcrun`, qualified names). Inbound: adapter -> parts -> MediaStore (workspace write) -> InboundMessage -> agent delivery. Outbound: agent parts -> adapter.send(parts) -> platform. Session identity and pairing sit in the gateway; session history sits in the agent workspace; the dashboard reads that history through arcgateway.fs_reader per SPEC-022 and holds none of it. See `.claude/steering/structure.md#module-boundaries` and `.claude/steering/tech.md#conventions`.

## Components

### COMP-001: InboundMessage + Part vocabulary
**Responsibility:** The single normalised envelope. An ordered list of typed parts (text, image, file, audio) plus routing identity. Text is a part, so media is never a branch.
**Dependencies:** _(none)_
**Inputs:** platform payload, resolved user_did/agent_did/session_key
**Outputs:** InboundMessage(parts: list[Part]) — MediaPart carries kind, mime, declared name, workspace ref; never bytes

### COMP-002: MediaStore
**Responsibility:** Own every artefact crossing the boundary: write inbound bytes to the workspace with direct filesystem I/O, compose the path itself, enforce the size ceiling, and emit one audit event per artefact stored or sent.
**Dependencies:** arctrust.audit, agent workspace
**Inputs:** bytes stream, declared mime/name, resolved sender, agent workspace root
**Outputs:** workspace path `inbox/<date>/<hhmmss>-<sender>-<stem>.<ext>`, or a typed refusal when over the ceiling; one media.received / media.sent AuditEvent

### COMP-003: AdapterRegistry
**Responsibility:** Discover adapters by scanning `arcgateway/adapters/` for a module-level PLATFORM descriptor. Skip a folder that is absent or fails to import, and log the roster that did load.
**Dependencies:** AdapterSpec
**Inputs:** adapters package directory
**Outputs:** list[AdapterSpec]; import failures logged, never raised to startup

### COMP-004: PlatformAdapter contract
**Responsibility:** The whole surface an adapter author implements: connection lifecycle, payload-to-parts, parts-to-platform, and a declaration of optional capabilities the gateway degrades around.
**Dependencies:** COMP-001
**Inputs:** connect()/disconnect(); on_payload -> list[Part] plus a fetch callable per media part; send(target, parts)
**Outputs:** AdapterSpec(name, requires, supports, build). Download, naming, caps, audit, session keys, pairing and splitting are NOT the adapter's to implement

### COMP-005: In-tree platform adapters
**Responsibility:** telegram, slack, mattermost and web as sibling folders under arcgateway/adapters/, replacing the separate distributions. Each handles all inbound kinds, not text alone.
**Dependencies:** COMP-004, COMP-003
**Inputs:** platform wire events including photo/document/voice
**Outputs:** parts in, parts out; degrades to a text description where the platform cannot carry a kind or size

### COMP-006: GatewayDelivery
**Responsibility:** Hand every inbound message to the agent's delivery entry point and stop deciding anything about runs. Replaces the queue-then-run branch and the per-session FIFO.
**Dependencies:** arcagent delivery API, COMP-007
**Inputs:** InboundMessage
**Outputs:** the agent's own outcome (injected / new turn / started); no interrupt flag crosses this seam, and no second waiting line exists in the gateway

### COMP-007: SessionIdentity
**Responsibility:** Sole owner of how a session key is derived and rotated, for every surface. Rotation only on the explicit new-session command.
**Dependencies:** _(none)_
**Inputs:** agent_did, user_did, platform, chat/thread
**Outputs:** stable session_key; new_session() rotates. arcui and arctui call this and derive none of their own

### COMP-008: BrokerBootstrap
**Responsibility:** Guarantee a message broker on every launch path as part of embedded gateway startup, supervising and tearing down any child it starts.
**Dependencies:** arcteam.nats_server.ensure_nats_server
**Inputs:** configured broker url, JetStream store dir
**Outputs:** a running or reused broker and a handle to stop; on failure a loud log and an explicit unavailable state on messaging routes — never a fabricated empty inbox

### COMP-009: PartTranslator
**Responsibility:** Turn a workspace media reference into a model content block at the agent's own boundary, reading bytes only for the call that needs them.
**Dependencies:** arcrun facade
**Inputs:** MediaPart(ref)
**Outputs:** an arcrun-exposed content block for this call only; the session log keeps the reference

### COMP-010: SessionHistory (existing)
**Responsibility:** Unchanged: the agent writes every turn to `<workspace>/sessions/<key>.jsonl`. Turns may now contain media parts, which are references and stay small.
**Dependencies:** _(none)_
**Inputs:** turn messages
**Outputs:** append-only jsonl, listable and replayable through arcgateway.fs_reader exactly as today

### COMP-011: Replay media line
**Responsibility:** Render a media part in session replay as a readable filename and kind.
**Dependencies:** COMP-010
**Inputs:** a replayed turn containing a media part
**Outputs:** a named-file line in the transcript; the file's bytes are never served to the browser

### COMP-012: TeamDelivery parity
**Responsibility:** Agent-to-agent messages travel the same delivery path as human ones, and acceptance means delivered or dead-lettered with a reason.
**Dependencies:** COMP-006, arcteam messaging
**Inputs:** signed teammate message
**Outputs:** same injection decision, session identity and audit shape as a human message; offline addressee retains and receives on next read; never a silent drop

### COMP-013: Adapter contract suite
**Responsibility:** Parametrised over every discovered adapter: inbound text, inbound image, inbound file, outbound long text, outbound media. Plus a per-adapter test driving a fake platform payload through the real handler, real gateway and real agent.
**Dependencies:** COMP-003, COMP-005
**Inputs:** fake platform payloads
**Outputs:** assertions on the workspace file, the session parts, and the delivery outcome. A text-only handler registration fails the image case

### COMP-014: Pairing (collapsed)
**Responsibility:** One module with one entry point replacing four files, preserving hashed user ids, operator approval and throttling. A paired channel is the authorization boundary for media on it.
**Dependencies:** arctrust
**Inputs:** platform user id, operator approval state
**Outputs:** paired / not paired; no per-artefact human gate on an already-approved channel


## Data Model

No database change. Two on-disk shapes. (1) Inbound artefacts at `<workspace>/inbox/<YYYY-MM-DD>/<hhmmss>-<sender>-<stem>.<ext>`, written with direct filesystem I/O per ADR-029 because an inbound attachment is agent state. Retention is deliberately unspecified and is an open question. (2) Session history at `<workspace>/sessions/<key>.jsonl`, unchanged in location and format; a turn's content may now be a list of blocks including a media reference, which the dashboard's renderer already dispatches over.

## External Integrations

Telegram Bot API, Slack, and Mattermost via their existing clients, now vendored as in-tree adapter folders rather than separate distributions. NATS/JetStream as the team message transport, auto-started by COMP-008 through the existing `arcteam.nats_server.ensure_nats_server`, which is present and working but currently has exactly one caller.

## Traceability

| Requirement | Components |
|---|---|
| REQ-296 | COMP-001 |
| REQ-297 | COMP-002 |
| REQ-298 | COMP-002 |
| REQ-299 | COMP-002 |
| REQ-300 | COMP-002 |
| REQ-301 | COMP-009 |
| REQ-302 | COMP-006 |
| REQ-303 | COMP-006 |
| REQ-304 | COMP-007 |
| REQ-305 | COMP-007, COMP-014 |
| REQ-306 | COMP-008 |
| REQ-307 | COMP-008 |
| REQ-308 | COMP-003, COMP-005 |
| REQ-309 | COMP-003 |
| REQ-310 | COMP-004 |
| REQ-311 | COMP-004, COMP-005 |
| REQ-312 | COMP-006, COMP-012 |
| REQ-313 | COMP-012 |
| REQ-314 | COMP-012 |
| REQ-315 | COMP-013 |
| REQ-316 | COMP-010 |
| REQ-317 | COMP-006, COMP-013 |
| REQ-318 | COMP-011 |

## Alternatives Considered

Inline base64 media in the envelope — rejected: the model API is stateless so the bytes re-ride every turn, the session log keeps them forever, and the agent cannot re-open the file next turn. A gateway-side blob store the agent fetches from — rejected: it puts agent-visible data outside the workspace and gives the agent a fetch dependency to read its own inbound file. Adapters as separate installable packages — rejected by the operator: a platform should be a folder you can delete. Each adapter owning its own download and storage — rejected: four adapters would be four implementations of the same size cap and the same filename handling. Gateway branches on active_run — rejected: it duplicates a decision the agent already makes and the two would drift. Keeping the gateway's per-session FIFO — rejected: once the agent serialises turns it is a second waiting line doing one job; its only remaining value was a 100-message flood cap, which belongs beside the agent's existing per-session lock if it is ever needed.

## Risks and Mitigations

This rewires the only path that makes agents reachable, so a regression is a total outage of the surface rather than a degraded feature. Mitigations: COMP-013 must be green for every discovered adapter before the phase merges, and session, runner and web stay untouched. Folding three adapter packages in-tree changes the install shape for anyone depending on them as distributions. COMP-008 introduces a supervised child process into gateway startup, which must be torn down with the parent or it outlives it. Workspace media grows without bound until retention is decided. The dashboard renderer already falls back to raw JSON for unknown block shapes, so media in history degrades to present-but-ugly rather than breaking the transcript, which is why COMP-011 is an addition rather than a repair.

## Open Questions

- Media retention: this design writes artefacts into the workspace and gives them no lifetime. Needs a decision before the media phase ships.
- Whether the 100-message flood cap lost with the gateway FIFO should be reinstated in the agent's session coordinator, or left out until flooding is observed.
- Whether the in-tree adapter move should keep import shims for the three existing distributions during a transition, which the no-legacy rule would otherwise forbid.
