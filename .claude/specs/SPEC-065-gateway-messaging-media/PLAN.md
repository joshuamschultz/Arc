# Implementation Plan: Gateway Messaging + Media

## Context References

- **PRD:** [PRD.md](./PRD.md)
- **SDD:** [SDD.md](./SDD.md)
- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Roadmap:** [.claude/steering/roadmap.md](../../steering/roadmap.md)

## Phase 1: Foundation

- [ ] **T-923**: (red) Adapter contract suite skeleton, parametrised over discovered adapters
  - domain: test
  - Components: COMP-013, COMP-003
  - Requirements: REQ-315, REQ-308
  - Acceptance: Suite enumerates adapters via the registry and runs its cases against each; with only the current text-only adapters registered, the inbound-image case FAILS. That failure is the photo defect, reproduced before any fix.
- [ ] **T-924**: (green) InboundMessage + Part vocabulary
  - domain: backend
  - Components: COMP-001
  - Requirements: REQ-296
  - Acceptance: A payload with text, an image and a file normalises to one ordered parts list; text is a part, not a separate field. Round-trips through the envelope without loss of order.
- [ ] **T-925**: (green) MediaStore: workspace write with gateway-composed path
  - domain: backend
  - Components: COMP-002
  - Requirements: REQ-297, REQ-298
  - Acceptance: Bytes land at inbox/<date>/<hhmmss>-<sender>-<stem>.<ext>. A sender filename containing separators or traversal cannot influence the path; the declared name survives as metadata only.
- [ ] **T-926**: (green) MediaStore: size ceiling refusal
  - domain: backend
  - Components: COMP-002
  - Requirements: REQ-299
  - Acceptance: An artefact over the ceiling is refused, nothing is written, and the refusal reaches the sender on the originating channel.
- [ ] **T-927**: (green) MediaStore: one audit event per artefact
  - domain: backend
  - Components: COMP-002
  - Requirements: REQ-300
  - Acceptance: Storing and sending each emit exactly one AuditEvent naming actor, channel, kind and size, through the arctrust chokepoint.
- [ ] **T-928**: (green) SessionIdentity as sole owner of keys and rotation
  - domain: backend
  - Components: COMP-007
  - Requirements: REQ-304
  - Acceptance: Key derivation and rotation live in one module; an architecture test asserts no surface derives its own. Rotation happens only via the new-session command.

## Phase 2: Core

- [ ] **T-929**: (red) Delivery test: a message during a live turn joins it
  - domain: test
  - Components: COMP-006
  - Requirements: REQ-302
  - Acceptance: With an interactive run in flight, a second message is injected into that run rather than starting another. Fails today because no human surface calls the agent's delivery entry point.
- [ ] **T-930**: (green) GatewayDelivery: hand every message to the agent
  - domain: backend
  - Components: COMP-006
  - Requirements: REQ-302, REQ-312
  - Acceptance: The executor calls the agent's delivery API for every inbound message. No interrupt flag crosses the seam and the gateway makes no run decision.
- [ ] **T-931**: (green) Background runs are never injection targets
  - domain: backend
  - Components: COMP-006
  - Requirements: REQ-303
  - Acceptance: With only a scheduled or consolidation run in flight, the message opens a new turn in the same session and the background run is not interrupted.
- [ ] **T-932**: (refactor) Delete the gateway per-session FIFO
  - domain: backend
  - Components: COMP-006, COMP-013
  - Requirements: REQ-317, REQ-315
  - Acceptance: QueueManager and its wiring are removed, not left unreachable. The full suite stays green, proving nothing else depended on it.
- [ ] **T-933**: (green) PartTranslator: workspace ref to content block at the agent boundary
  - domain: backend
  - Components: COMP-009
  - Requirements: REQ-301
  - Acceptance: A media part becomes a model content block for one call only, reached through the arcrun facade. arcgateway imports no model package; the architecture guard proves it.
- [ ] **T-934**: (green) Session history carries references, not bytes
  - domain: backend
  - Components: COMP-010, COMP-009
  - Requirements: REQ-316, REQ-301
  - Acceptance: A turn with a 5MB image adds kilobytes to the jsonl, not megabytes, and the file is re-openable on a later turn.
- [ ] **T-935**: (red) Session listing and replay unchanged
  - domain: test
  - Components: COMP-010
  - Requirements: REQ-316
  - Acceptance: The existing dashboard list and replay endpoints return the same shape for text-only sessions as before this spec. Regression guard for the operator's constraint.
- [ ] **T-936**: (green) Replay names media files
  - domain: ui
  - Components: COMP-011
  - Requirements: REQ-318
  - Acceptance: A turn containing a media part renders its filename and kind as a readable line. No route serves workspace bytes to the browser.

## Phase 3: Integration

- [ ] **T-937**: (green) AdapterRegistry: directory scan against a declared descriptor
  - domain: infra
  - Components: COMP-003
  - Requirements: REQ-308
  - Acceptance: Adapters are discovered by scanning the adapters package for PLATFORM. Adding a folder makes it available with no registry edit.
- [ ] **T-938**: (green) A missing or broken adapter never blocks startup
  - domain: infra
  - Components: COMP-003
  - Requirements: REQ-309
  - Acceptance: Deleting a folder, and a folder that raises on import, both leave the gateway starting normally with the loaded roster logged.
- [ ] **T-939**: (green) PlatformAdapter contract + capability declarations
  - domain: backend
  - Components: COMP-004
  - Requirements: REQ-310
  - Acceptance: The adapter implements only lifecycle, payload-to-parts and send-parts. Optional capabilities are declared and the gateway degrades when absent.
- [ ] **T-940**: (refactor) Move telegram, slack and mattermost in-tree
  - domain: infra
  - Components: COMP-005, COMP-003
  - Requirements: REQ-308
  - Acceptance: The three packages become adapter folders and the distributions are deleted, not shimmed. Each handles every inbound kind, so the suite's image case passes.
- [ ] **T-941**: (green) Outbound parts, with per-platform degradation
  - domain: backend
  - Components: COMP-004, COMP-005
  - Requirements: REQ-311
  - Acceptance: An agent reply containing a file is delivered through send(parts). Where the platform cannot carry the kind or size, a text description is sent and the turn is not lost.
- [ ] **T-942**: (green) Broker guaranteed on every launch path
  - domain: infra
  - Components: COMP-008
  - Requirements: REQ-306
  - Acceptance: Embedded gateway startup ensures a broker, reusing one already running. Messages works from a plain start with no operator step; any child started is torn down with the parent.
- [ ] **T-943**: (green) Broker absent reports unavailable, never an empty inbox
  - domain: infra
  - Components: COMP-008
  - Requirements: REQ-307
  - Acceptance: With no broker reachable, messaging routes return an explicit unavailable state and the log says so loudly. Tested with a mocked backend, no live broker required.
- [ ] **T-944**: (green) Agent-to-agent parity on the unified path
  - domain: backend
  - Components: COMP-012, COMP-006
  - Requirements: REQ-312
  - Acceptance: A teammate message and a human message with identical content produce the same injection decision, session identity and audit shape.
- [ ] **T-945**: (green) Accepted means delivered or dead-lettered
  - domain: backend
  - Components: COMP-012
  - Requirements: REQ-313
  - Acceptance: Every accepted message is delivered or recorded in the dead-letter path with a reason. No path drops one silently.
- [ ] **T-946**: (green) Offline addressee retains and receives
  - domain: backend
  - Components: COMP-012
  - Requirements: REQ-314
  - Acceptance: A message to an offline agent is retained and delivered when that agent next reads, rather than lost with the sender's turn.

## Phase 4: Polish

- [ ] **T-947**: (green) Paired channel is the authorization boundary
  - domain: auth
  - Components: COMP-014, COMP-007
  - Requirements: REQ-305
  - Acceptance: Media on an already-paired or operator-authenticated channel raises no per-artefact human gate. An unpaired channel still gets no agent response at all.
- [ ] **T-948**: (refactor) Collapse pairing to one module
  - domain: auth
  - Components: COMP-014
  - Requirements: REQ-305
  - Acceptance: Four files become one entry point with hashed ids, operator approval and throttling intact. The pairing suite passes unchanged; session, runner and web are untouched.

## Traceability

| Requirement | Tasks |
|---|---|
| REQ-296 | T-924 |
| REQ-297 | T-925 |
| REQ-298 | T-925 |
| REQ-299 | T-926 |
| REQ-300 | T-927 |
| REQ-301 | T-933, T-934 |
| REQ-302 | T-929, T-930 |
| REQ-303 | T-931 |
| REQ-304 | T-928 |
| REQ-305 | T-947, T-948 |
| REQ-306 | T-942 |
| REQ-307 | T-943 |
| REQ-308 | T-923, T-937, T-940 |
| REQ-309 | T-938 |
| REQ-310 | T-939 |
| REQ-311 | T-941 |
| REQ-312 | T-930, T-944 |
| REQ-313 | T-945 |
| REQ-314 | T-946 |
| REQ-315 | T-923, T-932 |
| REQ-316 | T-934, T-935 |
| REQ-317 | T-932 |
| REQ-318 | T-936 |

## Open Questions

- Media retention: artefacts are written with no lifetime. Decide before Phase 1 ships or the inbox grows forever.
- The 100-message flood cap disappears with T-932. Reinstate it beside the agent's per-session lock only if flooding is actually observed.
- T-940 deletes the three adapter distributions outright. Confirm nothing outside this repo installs them before that task runs.
