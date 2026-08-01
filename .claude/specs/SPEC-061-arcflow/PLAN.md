# Implementation Plan: ArcFlow — Named, Signed, Conversationally-Authored Workflows

## Context References

- **PRD:** [PRD.md](./PRD.md)
- **SDD:** [SDD.md](./SDD.md)
- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Roadmap:** [.claude/steering/roadmap.md](../../steering/roadmap.md)

## Phase 1: Foundation

- [x] **T-826**: (red) WorkflowDefinition model tests: five node kinds, needs, join, loops, schema_version
  - domain: test
  - Components: COMP-001
  - Requirements: REQ-217, REQ-218
  - Acceptance: Tests assert every node kind parses with its required fields; an unknown schema_version major is refused; frozen models reject mutation. Tests fail against no implementation.
- [x] **T-827**: (green) WorkflowDefinition models
  - domain: backend
  - Components: COMP-001
  - Requirements: REQ-217, REQ-218
  - Acceptance: Models are frozen, typed, and carry no LLM wire-control fields (no model or temperature). The model tests pass.
- [x] **T-828**: (red) PredicateEvaluator tests: whitelisted AST only, no calls or attribute access
  - domain: test
  - Components: COMP-003
  - Requirements: REQ-219
  - Acceptance: Comparisons and boolean combinations over node outputs and run input evaluate correctly; a function call, attribute access, import, or environment read is a parse error, not an evaluation.
- [x] **T-829**: (green) PredicateEvaluator
  - domain: backend
  - Components: COMP-003
  - Requirements: REQ-219
  - Acceptance: Recursive-descent parser produces a whitelisted AST behind one evaluate seam; no eval anywhere. The predicate tests pass.
- [x] **T-830**: (red) GraphValidator tests: the join deadlock, cycles, dangling refs, unsatisfiable outputs, quotas
  - domain: test
  - Components: COMP-002
  - Requirements: REQ-219, REQ-220
  - Acceptance: A node whose needs span exclusive router routes without join=any is rejected; an undeclared cycle is rejected; a loop without max_iterations is rejected; an output reference from a node that cannot co-occur with its source is rejected; definition and node-count quotas are enforced. Errors carry node id, field, observed value, and admissible alternatives.
- [x] **T-831**: (green) GraphValidator with SCC analysis and typed error list
  - domain: backend
  - Components: COMP-002
  - Requirements: REQ-219, REQ-220
  - Acceptance: Every SCC larger than one node is entered by exactly one declared back-edge sharing one counter; nested and overlapping loops are refused. The validator tests pass.
- [x] **T-832**: (red) Canonical hashing and manifest tests: reformat-stable, file-drift detected
  - domain: test
  - Components: COMP-005
  - Requirements: REQ-226, REQ-227
  - Acceptance: Two byte-different but semantically identical documents hash identically; changing any referenced schema, prompt, or script file changes the manifest hash and fails verification closed.
- [x] **T-833**: (green) DefinitionStore: canonical hash, file manifest, operator-pinned verification, tier gate
  - domain: auth
  - Components: COMP-005
  - Requirements: REQ-225, REQ-226, REQ-227
  - Acceptance: Verification pins the operator key rather than trusting any valid signature; unsigned or foreign-signed definitions are refused above personal tier and warn at personal tier. The hashing and manifest tests pass.
- [x] **T-834**: (red) Draft-status invariant test: validation success never confers signed status
  - domain: test
  - Components: COMP-005, COMP-012
  - Requirements: REQ-223
  - Acceptance: A fully valid definition produced through every authoring path is status=draft; no code path sets signed status outside the operator signing command. This test is the security gate for the whole feature.
- [x] **T-835**: (red) Run store tests: lifecycle, path taken, conditional transitions
  - domain: test
  - Components: COMP-006
  - Requirements: REQ-228
  - Acceptance: Run records persist workflow id, version, content hash, initiator, budget, and an ordered path taken; status transitions are conditional so two writers cannot both advance the same run.
- [x] **T-836**: (green) Run store on the shared mutable plane
  - domain: db
  - Components: COMP-006
  - Requirements: REQ-228, REQ-236
  - Acceptance: Its own collection, using the existing conditional-update primitive; no change to the closed spool kind set. The run store tests pass.
- [x] **T-837**: (red) Batch task creation tests: cross-owner atomicity and idempotency
  - domain: test
  - Components: COMP-007
  - Requirements: REQ-229, REQ-234
  - Acceptance: A frontier batch spanning several owning agents commits atomically; replaying the same batch after a simulated crash returns existing rows rather than creating duplicates.
- [x] **T-838**: (green) Batch task creation keyed on run and node identity
  - domain: db
  - Components: COMP-007
  - Requirements: REQ-229, REQ-234
  - Acceptance: New store capability, additive to the existing task model. The batch creation tests pass.
- [x] **T-839**: (green) Runner identity resolution
  - domain: auth
  - Components: COMP-011
  - Requirements: REQ-232
  - Acceptance: The runner resolves its own actor identity from the on-disk key path without generating one, fails closed when absent, and is distinct from any agent identity and from the shared dashboard operator identity.

## Phase 2: Core

- [x] **T-840**: (red) ValueResolver tests: typed binding, never textual interpolation
  - domain: test
  - Components: COMP-004
  - Requirements: REQ-239
  - Acceptance: References bind as typed values into tool arguments and prompt sections; a payload containing command or prompt metacharacters is passed as data and never concatenated into a command string.
- [x] **T-841**: (green) ValueResolver
  - domain: backend
  - Components: COMP-004
  - Requirements: REQ-239
  - Acceptance: Resolution reads only validated upstream outputs recorded on task rows. The resolver tests pass.
- [x] **T-842**: (red) Runner tests: lazy materialization, routers, loops, path taken, cancel ordering
  - domain: test
  - Components: COMP-008
  - Requirements: REQ-220, REQ-229, REQ-233
  - Acceptance: Only reachable nodes materialize; untaken branches produce no task rows; a loop mints iteration-stamped rows up to its bound then fails the node; a completed node is never re-executed on resume.
- [x] **T-843**: (green) WorkflowRunner: frontier advancement, routing, loops, wiring, budget
  - domain: backend
  - Components: COMP-008
  - Requirements: REQ-218, REQ-220, REQ-229
  - Acceptance: Deterministic code with no LLM call; receives tier at construction; queries scoped by run rather than listing and filtering. The runner frontier tests pass.
- [x] **T-844**: (red) Node execution adapter tests: schema validation and artifact checks actually fire
  - domain: test
  - Components: COMP-014
  - Requirements: REQ-237, REQ-238
  - Acceptance: A node completing with output violating its schema is a retryable failure, not a pass-forward; a node whose declared artifacts are absent fails with a message naming the producing tool. Exercised through the real completion path, not a unit stub.
- [x] **T-845**: (green) Node execution adapter: prompt injection seam, skill activation, strategy, schema, artifacts, idempotency key
  - domain: ai-workflow
  - Components: COMP-014
  - Requirements: REQ-233, REQ-243
  - Acceptance: Upstream outputs reach the node through the assemble-prompt seam rather than direct context mutation; a declared skill activates deterministically; a declared strategy list reaches the loop and an absent one pins the reactive strategy.
- [x] **T-846**: (red) Capability-leg threading test: accumulation survives a fresh node session
  - domain: test
  - Components: COMP-015
  - Requirements: REQ-240
  - Acceptance: A run whose first node touches private data and whose second node attempts external communication is denied at the second node, proving accumulated legs reach the policy context across session boundaries. Uses the real policy pipeline, not a mock.
- [x] **T-847**: (green) Capability-leg threader with bounded accumulation
  - domain: auth
  - Components: COMP-015
  - Requirements: REQ-240
  - Acceptance: T-846 passes. The accumulated set is bounded and its growth is observable.
- [x] **T-848**: (green) Activation approval for capability-spanning definitions
  - domain: auth
  - Components: COMP-016
  - Requirements: REQ-241
  - Acceptance: A definition whose nodes jointly span a forbidden composition requires an operator-signed grant bound to its content hash at first activation and after any widening edit; a narrowing edit does not re-prompt.
- [x] **T-849**: (red) Builder tool tests: allowlists, quotas, ordering, typed repairable errors
  - domain: test
  - Components: COMP-012
  - Requirements: REQ-221, REQ-222
  - Acceptance: Each mutating tool ignores fields outside its allowlist; quotas are checked before validation work; the whole graph validates in memory and cycles are checked before any write; a stale expected version is refused rather than merged; errors carry node id, field, observed value, and admissible alternatives.
- [x] **T-850**: (green) Workflow builder tools in the agent module
  - domain: api
  - Components: COMP-012
  - Requirements: REQ-221, REQ-223
  - Acceptance: Create, node, trigger, channel, run, cancel, and read-only inspection tools; every mutation produces a draft; inline free text is normalized before any injection check. The builder tool tests and the draft-status invariant test pass.
- [x] **T-851**: (green) workflow-builder skill
  - domain: ai-workflow
  - Components: COMP-013
  - Requirements: REQ-221
  - Acceptance: Follows the repository's seven-section skill format with no filler sections; covers what to elicit, node-kind selection, the named coordination patterns, and the anti-patterns to refuse including building a workflow for something that runs once.
- [x] **T-860**: (green) Runner: resume without re-execution, and ordered cancellation
  - domain: backend
  - Components: COMP-008
  - Requirements: REQ-233, REQ-234, REQ-235
  - Acceptance: A restart mid-materialization re-derives already-materialized nodes rather than duplicating them; a completed node's recorded output is replayed rather than recomputed; a cancel marks the run before fanning out so a node completing during the sweep cannot extend the frontier.
- [x] **T-861**: (green) Runner: run-level budget, stall detection, and terminal roll-up
  - domain: backend
  - Components: COMP-006, COMP-008
  - Requirements: REQ-228, REQ-236
  - Acceptance: Token and wall-clock budget is enforced across all nodes of a run with reserve-then-settle accounting and terminates the run on exhaustion; a run with no progress and nothing in flight is escalated rather than sitting silent; node terminal states roll into the Run record.
- [x] **T-862**: (green) Node adapter: output-schema enforcement, artifact checks, idempotency key
  - domain: ai-workflow
  - Components: COMP-014
  - Requirements: REQ-237, REQ-238, REQ-242
  - Acceptance: Output violating a declared schema is a retryable node failure rather than a pass-forward; missing declared artifacts fail with a message naming the producing tool; the per-attempt idempotency key reaches tool dispatch so a retry cannot repeat an external side effect.
- [x] **T-863**: (green) Builder tools: versioned editing with optimistic concurrency
  - domain: api
  - Components: COMP-005, COMP-012
  - Requirements: REQ-222, REQ-248
  - Acceptance: An edit requires the editor's expected version, refuses a stale edit rather than merging it, increments the version, returns the definition to draft, and audits the actor and reason; rejection errors carry node id, field, observed value, and admissible alternatives.
- [x] **T-866**: (red) Handoff invariant: a run completes with every outbound message dropped
  - domain: test
  - Components: COMP-008, COMP-010
  - Requirements: REQ-251
  - Acceptance: With the messenger stubbed to drop every send, a multi-agent run still advances node to node and reaches a terminal state, proving no progress depends on message delivery. A companion assertion proves no node's work content is read from a message body: task rows and validated upstream outputs are the only inputs to a node's prompt.
- [x] **T-867**: (green) Handoff writes name exactly one owner and are atomically claimed
  - domain: backend
  - Components: COMP-007, COMP-008
  - Requirements: REQ-251
  - Acceptance: Each materialized node row carries the owner named by the definition; two agents racing the same row produce exactly one claim; an undelivered wake signal costs only latency because the owning agent's dispatch loop finds the row on its next tick.
- [x] **T-868**: (green) WorkflowControlPlane: one shared operation set for every surface
  - domain: backend
  - Components: COMP-021
  - Requirements: REQ-252, REQ-253
  - Acceptance: Create, edit, archive, unarchive, run, and cancel exist once, owning validation, versioning, draft lifecycle, and audit emission; the agent builder tools, the command line, and the dashboard all invoke these same operations with no surface-specific logic.
- [x] **T-870**: (green) Archive semantics: hide and disable without erasing history
  - domain: backend
  - Components: COMP-022
  - Requirements: REQ-255
  - Acceptance: Archiving hides a workflow from the active list, disables its trigger, and refuses new runs while retaining the bundle, every prior version, and all run history so past runs stay renderable; unarchive restores it as a draft.
- [x] **T-871**: (green) Purge guard: refuse while runs reference the definition
  - domain: backend
  - Components: COMP-022
  - Requirements: REQ-256
  - Acceptance: A purge is refused while any run still references the workflow; a forced purge records in the audit chain that history for that workflow is henceforth unrenderable.
- [x] **T-875**: (green) Run workspace: the shared desk for one run
  - domain: backend
  - Components: COMP-024
  - Requirements: REQ-258
  - Acceptance: Each run gets <team_root>/shared/runs/<run_id>/; every node's agent works there for its node; an agent's private workspace is never touched; the folder is created once and reused across nodes and resumes.
- [x] **T-876**: (red) Artifact paths resolve inside the run workspace by construction
  - domain: test
  - Components: COMP-024, COMP-014
  - Requirements: REQ-258, REQ-259
  - Acceptance: A declared artifact resolves relative to the run workspace; a path that would resolve outside it is refused; the refusal holds for a hand-edited bundle that never passed the authoring validator. Mutation-verified: deleting the check must turn a test red.

## Phase 3: Integration

- [x] **T-852**: (green) Runner host in the fleet service with singleton enforcement
  - domain: infra
  - Components: COMP-009
  - Requirements: REQ-230, REQ-231
  - Acceptance: The runner starts on the agent side of the fleet service; a second instance refuses to start rather than racing the frontier; a headless deployment with no dashboard still progresses runs to completion.
- [x] **T-853**: (refactor) Layer model and dependency declarations updated
  - domain: infra
  - Components: COMP-009
  - Requirements: REQ-230
  - Acceptance: The structure steering document shows the store package and the new downward edge; the gateway package declares its new dependencies; the agent package's dependency posture is decided and recorded; architecture tests pass.
- [x] **T-854**: (green) Channel binding and run narration
  - domain: backend
  - Components: COMP-010
  - Requirements: REQ-244
  - Acceptance: Node start, completion, handoff, gate waiting, gate resolution, and run outcome post to the bound channel as narration that is recorded and displayed but never activates an agent.
- [x] **T-855**: (green) Gate resolution: control-plane only, in-channel, reviewer chooses outcome
  - domain: ui
  - Components: COMP-018
  - Requirements: REQ-245, REQ-246, REQ-247
  - Acceptance: An answerable card renders in the bound channel; resolution records the deciding human's authenticated identity; the reviewer chooses failing the run or returning it for revision with notes; no agent-callable tool can resolve a gate, asserted by test.
- [x] **T-856**: (green) Typed schedule trigger, tested through the real firing path
  - domain: backend
  - Components: COMP-017
  - Requirements: REQ-249
  - Acceptance: A due schedule with a workflow action starts a real run with no model in the decision path; an overlapping firing skips while one is in flight; repeated failures trip the existing breaker; quiet hours suppress firing. Test drives the scheduler tick, not the action handler directly.
- [x] **T-857**: (green) Operator signing and workflow CLI
  - domain: api
  - Components: COMP-019
  - Requirements: REQ-224
  - Acceptance: Sign, verify, list, show, run, and cancel work from the operator surface; the signing key is resolved only here and never inside an agent process, asserted by test.
- [ ] **T-858**: (green) End-to-end: describe, sign, run twice, identical path
  - domain: test
  - Components: COMP-005, COMP-006, COMP-008, COMP-012, COMP-014, COMP-019
  - Requirements: REQ-217, REQ-224, REQ-228
  - Acceptance: A three-node multi-agent workflow is authored through builder tools, signed out-of-band, and run; version, path taken, signer, and gate approvers are reconstructible from durable signed records.
- [ ] **T-864**: (green) End-to-end: two runs of one signed workflow produce identical paths
  - domain: test
  - Components: COMP-008, COMP-014
  - Requirements: REQ-233, REQ-237
  - Acceptance: The same signed workflow run twice records identical node sequences; a resumed run does not re-execute completed nodes; a schema-violating node output fails the run rather than propagating. Driven through the real dispatch path.
- [ ] **T-869**: (red) Removability test: every capability works with the dashboard uninstalled
  - domain: test
  - Components: COMP-019, COMP-021
  - Requirements: REQ-257
  - Acceptance: With arcui absent, create, view, edit, archive, purge, run, and cancel all succeed from the command line and from agent tools, and a run completes end to end. Proves the dashboard shows actions that exist anyway rather than owning any of them.
- [x] **T-872**: (green) Dashboard workflow routes: thin delegating adapter
  - domain: api
  - Components: COMP-023
  - Requirements: REQ-253, REQ-254
  - Acceptance: Routes authenticate the operator, translate HTTP to a control-plane operation, and return its result verbatim with a boundary audit record; a test asserts the route layer contains no validation, versioning, sequencing, signing, or execution logic of its own.
- [x] **T-873**: (green) Dashboard management surface: list, create, edit, archive, run
  - domain: ui
  - Components: COMP-020
  - Requirements: REQ-252
  - Acceptance: An operator creates a workflow, edits its nodes and trigger, archives it, and starts a run entirely from the dashboard without touching a file or a command; validation errors render against the offending node and field; the surface offers no way to sign.
- [x] **T-874**: (green) Definition graph and live run status views
  - domain: ui
  - Components: COMP-020
  - Requirements: REQ-250
  - Acceptance: The definition renders as a graph with version history showing signer and reason; a live run shows per-node status with untaken branches and loop iterations sourced from the run's path taken; a node click opens the existing per-run timeline. Graph code is route-split; the static bundle is rebuilt and the service restarted.

## Phase 4: Polish

- [ ] **T-865**: (refactor) Refactor: scoped runner queries and bounded accumulation
  - domain: backend
  - Components: COMP-008, COMP-015
  - Requirements: REQ-236
  - Acceptance: Runner ticks query by run rather than listing and filtering owned rows; per-run capability-leg accumulation is bounded with observable growth; all tests remain green.

## Traceability

| Requirement | Tasks |
|---|---|
| REQ-217 | T-826, T-827, T-858 |
| REQ-218 | T-826, T-827, T-843 |
| REQ-219 | T-828, T-829, T-830, T-831 |
| REQ-220 | T-830, T-831, T-842, T-843 |
| REQ-221 | T-849, T-850, T-851 |
| REQ-222 | T-849, T-863 |
| REQ-223 | T-834, T-850 |
| REQ-224 | T-857, T-858 |
| REQ-225 | T-833 |
| REQ-226 | T-832, T-833 |
| REQ-227 | T-832, T-833 |
| REQ-228 | T-835, T-836, T-858, T-861 |
| REQ-229 | T-837, T-838, T-842, T-843 |
| REQ-230 | T-852, T-853 |
| REQ-231 | T-852 |
| REQ-232 | T-839 |
| REQ-233 | T-842, T-845, T-860, T-864 |
| REQ-234 | T-837, T-838, T-860 |
| REQ-235 | T-860 |
| REQ-236 | T-836, T-861, T-865 |
| REQ-237 | T-844, T-862, T-864 |
| REQ-238 | T-844, T-862 |
| REQ-239 | T-840, T-841 |
| REQ-240 | T-846, T-847 |
| REQ-241 | T-848 |
| REQ-242 | T-862 |
| REQ-243 | T-845 |
| REQ-244 | T-854 |
| REQ-245 | T-855 |
| REQ-246 | T-855 |
| REQ-247 | T-855 |
| REQ-248 | T-863 |
| REQ-249 | T-856 |
| REQ-250 | T-874 |
| REQ-251 | T-866, T-867 |
| REQ-252 | T-868, T-873 |
| REQ-253 | T-868, T-872 |
| REQ-254 | T-872 |
| REQ-255 | T-870 |
| REQ-256 | T-871 |
| REQ-257 | T-869 |
| REQ-258 | T-875, T-876 |
| REQ-259 | T-876 |

## Open Questions

- Channel binding granularity: one channel per workflow with a thread per run, or a channel per run?
- Dashboard editor scope: full graph editing, or node-property editing over a rendered graph first?
- Which event trigger lands first after v1 — message, webhook, or task-created?
- Dependency posture: declare the store and team packages in the agent package metadata, or keep lazy imports with graceful degradation?
