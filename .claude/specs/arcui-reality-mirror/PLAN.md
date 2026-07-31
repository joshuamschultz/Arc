# Implementation Plan: ArcUI Reality Mirror

## Context References

- **PRD:** [PRD.md](./PRD.md)
- **SDD:** [SDD.md](./SDD.md)
- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Roadmap:** [.claude/steering/roadmap.md](../../steering/roadmap.md)

## Phase 1: Foundation

- **T-702** (red): arcmemory.operator facade — failing tests (list/search/get/links/edit/delete over a fixture index.db)
  - domain: test
  - Components: COMP-001
  - Requirements: REQ-084, REQ-087, REQ-100
  - Acceptance: RED: tests describe typed records with created/score/decay/source metadata, link traversal, search ranking passthrough, honest MutationResult on failure; fixture DB built via arcmemory's own capture path, not hand-inserted SQL
- **T-703** (green): arcmemory.operator facade implementation (adds minimal store methods inside arcmemory where gaps exist)
  - domain: backend
  - Components: COMP-001
  - Requirements: REQ-085, REQ-086, REQ-089
  - Acceptance: GREEN: the paired RED tests pass; facade is the only new public surface; arcmemory suite fully green; mypy --strict + ruff clean
- **T-704** (red): arcagent capability inventory seam — failing tests (four scan roots, verdicts verbatim incl. tofu-denied fixture)
  - domain: test
  - Components: COMP-007
  - Requirements: REQ-093, REQ-094
  - Acceptance: RED: tests cover a builtin, a global, an agent-dir, and a workspace skill + a tofu-denied item; status strings come from the loader result object, no literals in the seam
- **T-705** (green): arcagent.capabilities.inventory implementation (non-core module)
  - domain: backend
  - Components: COMP-007
  - Requirements: REQ-096
  - Acceptance: GREEN: the paired RED tests pass; arcagent suite green; core LOC budget unchanged; mypy --strict + ruff clean
- **T-706** (green): Embedded messaging-service wiring in arcui server lifespan — failing tests then implementation
  - domain: backend
  - Components: COMP-004
  - Requirements: REQ-090
  - Acceptance: RED then GREEN: with a team root, app.state.messaging_service is a live handle and /api/team/channels returns real channels; without a working service the route returns an explicit service_unavailable error payload (test asserts NOT []); service closed on shutdown; arcgateway core untouched

## Phase 2: Core

- **T-707** (green): UI mutation audit helper — tests + implementation
  - domain: backend
  - Components: COMP-010
  - Requirements: REQ-088, REQ-091, REQ-092
  - Acceptance: Single emission point wrapping the existing audit seam; test proves an event with actor/target/operation/outcome lands in the sink
- **T-708** (red): Knowledge routes — failing tests (viewer reads, operator mutations, 403s, empty-vs-unreadable, audit emission)
  - domain: test
  - Components: COMP-002
  - Requirements: REQ-084, REQ-097
  - Acceptance: RED: route tests drive the real Starlette app with real facade over a fixture DB; viewer mutation → 403; arcmemory error surfaces verbatim; audit event asserted
- **T-709** (green): Knowledge routes implementation
  - domain: api
  - Components: COMP-002
  - Requirements: REQ-085, REQ-086, REQ-100
  - Acceptance: GREEN: the paired RED tests pass; arcui backend suite green (chat_ws integration hang excluded — pre-existing); mypy --strict + ruff clean
- **T-710** (green): Channel management routes — failing tests then implementation (create/duplicate-409/membership, operator-gated, audited)
  - domain: api
  - Components: COMP-005
  - Requirements: REQ-091, REQ-092
  - Acceptance: RED then GREEN: create wraps the same service call as arc team create-channel; duplicate name → 409 (CLI parity); member add/remove resolves refs via registry; viewer → 403; audit events asserted
- **T-711** (green): Capability routes replacing stale glob — failing tests then implementation
  - domain: api
  - Components: COMP-008
  - Requirements: REQ-093, REQ-095, REQ-096
  - Acceptance: RED then GREEN: per-agent + fleet endpoints return COMP-007 inventory + full runtime tool registry; _read_agent_skills glob deleted (no fallback shim); tests include a tofu-denied item rendered with its verbatim status
- **T-716** (green): Agent workspace file editor routes — failing tests then implementation (confinement + secret guard + audit on save)
  - domain: api
  - Components: COMP-012, COMP-010
  - Requirements: REQ-099
  - Acceptance: RED then GREEN: GET returns file content; PUT saves under operator token with canonical-path confinement (escape -> 400), secret-content guard applied, audit event asserted; viewer PUT -> 403

## Phase 3: Integration

- **T-712** (green): Frontend: Knowledge view (browse/search/detail/links, metadata columns, operator edit/delete, empty-vs-error states)
  - domain: ui
  - Components: COMP-003
  - Requirements: REQ-085, REQ-097
  - Acceptance: tsc + eslint clean; view renders fixture data in dev; mutation controls hidden for viewer role
- **T-713** (green): Frontend: Channels management + Capabilities views (source/status badges, verbatim status strings) + bundle rebuild
  - domain: ui
  - Components: COMP-006, COMP-009
  - Requirements: REQ-094, REQ-095
  - Acceptance: tsc + eslint clean; vite build reproducible; new bundle committed to src/arcui/static; service-unavailable state visually distinct from empty
- **T-714** (green): Live validation on the DGX reference deployment (mirror-fidelity checks)
  - domain: mixed
  - Components: COMP-002, COMP-004, COMP-005, COMP-008
  - Requirements: REQ-090, REQ-095
  - Acceptance: UI channels == `arc team channels` 1:1; UI skills == `arc agent skills` + loader verdicts 1:1 (browserbase visible with its real status); josh_agent Knowledge view renders real index.db entries; a UI channel-create round-trips and is then removed or kept per operator choice
- **T-717** (green): Frontend: workspace file editor (tree, markdown view, edit mode operator-only)
  - domain: ui
  - Components: COMP-012
  - Requirements: REQ-099
  - Acceptance: tsc + eslint clean; identity.md viewable rendered and editable raw for operator; save errors surfaced verbatim

## Phase 4: Polish

- **T-715** (refactor): Docs, README ArcUI section, changelogs; refactor sweep
  - domain: mixed
  - Components: COMP-011
  - Requirements: REQ-098
  - Acceptance: single-node.md + team-building.md describe the new views; README ArcUI section updated; arcui CHANGELOG + root CHANGELOG entries; dead code from replaced views removed; all suites green after sweep

## Traceability

| Requirement | Tasks |
|---|---|
| REQ-084 | T-702, T-708 |
| REQ-085 | T-703, T-709, T-712 |
| REQ-086 | T-703, T-709 |
| REQ-087 | T-702 |
| REQ-088 | T-707 |
| REQ-089 | T-703 |
| REQ-090 | T-706, T-714 |
| REQ-091 | T-707, T-710 |
| REQ-092 | T-707, T-710 |
| REQ-093 | T-704, T-711 |
| REQ-094 | T-704, T-713 |
| REQ-095 | T-711, T-713, T-714 |
| REQ-096 | T-705, T-711 |
| REQ-097 | T-708, T-712 |
| REQ-098 | T-715 |
| REQ-099 | T-716, T-717 |
| REQ-100 | T-702, T-709 |

## Open Questions

_(none)_
