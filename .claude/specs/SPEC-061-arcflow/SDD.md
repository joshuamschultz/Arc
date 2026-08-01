# Solution Design Document: ArcFlow — Named, Signed, Conversationally-Authored Workflows

## Context References

- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Project structure:** [.claude/steering/structure.md](../../steering/structure.md)
- **PRD:** [PRD.md](./PRD.md)

## Overview

ArcFlow adds one missing layer, not a new engine: a named, signed workflow definition that instantiates onto Arc's existing task DAG. The engine (models, validator, runner, run coordination) lives in `arcteam` because a workflow is narrowed multi-agent coordination and arcteam owns that layer; it is a verified true leaf today (deps: arctrust only among siblings) so gaining `arcstore` is a legal downward edge that no architecture guard rejects. Execution reuses `arcstore.tasks` wholesale — atomic claims, dependency gating, retry/backoff/dead-letter, review gates, signed cross-agent handoff. Signing reuses `arctrust.artifact` and the `.arcsig` operator-pinned rail. The runner is hosted in `arcgateway` bootstrap, on the agent side of the fleet service, so execution never depends on the dashboard.

## Architecture

Layering per `.claude/steering/structure.md#layer-model`, with one new downward edge (`arcteam` → `arcstore`) that must be added to that document in the same change. Flow: an operator or agent authors a draft through builder tools (arcagent) / an IDE / the dashboard editor → the arcteam validator accepts or returns typed errors → the operator signs out-of-band via arccli, whose key never enters an agent process → a trigger (typed schedule action, tool call, CLI, or dashboard) starts a run → the arcteam runner in arcgateway creates a Run record, lazily materializes the ready frontier into arcstore task rows owned per node, and narrates to the bound channel → each agent's existing dispatch loop claims and executes its own node → arcagent validates the node's output against its schema and injects upstream outputs into the next node's prompt via the assemble-prompt seam → the runner advances the frontier, evaluates routers and loops, threads accumulated capability legs, and rolls terminal state into the Run record. Gates are review-status tasks resolvable only through the control plane. One rule governs the dashboard throughout: it views and it initiates, but it holds no operational work — every mutation and every run start is a delegated call to the same control-plane operation the command line invokes, and deleting the dashboard would remove no capability. Removability is the test of that rule: every workflow capability is reachable from the command line (COMP-019) and from agent conversation (COMP-012), and the system is expected to run correctly with arcui uninstalled — the dashboard shows actions that exist anyway.

## Components

### COMP-001: WorkflowDefinition models (arcteam)
**Responsibility:** Typed model of the canonical artifact: workflow header, trigger, input schema reference, and the node list across five kinds (agent/tool/script/router/gate) with needs, join, when, loop_back_to, max_iterations, output_schema, artifacts, strategy, and per-node agent assignment. Also carries schema_version for language evolution.
**Dependencies:** pydantic
**Inputs:** parsed workflow.toml document
**Outputs:** WorkflowDefinition (frozen) or a typed parse error list

### COMP-002: GraphValidator (arcteam)
**Responsibility:** Whole-graph static validation before any write or run: dangling needs, undeclared cycles via SCC analysis, loops entered by exactly one declared back-edge with a mandatory bound, join legality against router exclusivity, unknown agent/tool/skill/script references, resolvable schema references, parseable predicates, statically satisfiable output references, and size/quota caps.
**Dependencies:** COMP-001
**Inputs:** WorkflowDefinition + a resolver for known agents/tools/skills
**Outputs:** ok, or list[{node_id, field, error, observed, admissible}]

### COMP-003: PredicateEvaluator (arcteam)
**Responsibility:** Small hand-written recursive-descent parser and evaluator for `when` predicates and route conditions over a whitelisted AST (path, literal, comparison, boolean). No calls, no attribute access, no environment reads.
**Dependencies:** _(none)_
**Inputs:** expression string + scope of typed node outputs and run input
**Outputs:** bool, or a parse error

### COMP-004: ValueResolver (arcteam)
**Responsibility:** Bind `$nodes.<id>.output.<field>` and `$input.<field>` references to typed values for tool arguments and prompt sections. Never performs textual interpolation into a command or prompt string.
**Dependencies:** COMP-001
**Inputs:** reference expression + validated upstream outputs
**Outputs:** typed value, or an unsatisfiable-reference error

### COMP-005: DefinitionStore and signing gate (arcteam)
**Responsibility:** Load, canonicalize, hash, and verify a definition bundle. Computes the content hash over a canonical serialization plus a sorted manifest of every referenced file; verifies the detached signature against the pinned operator key; enforces tier-graded refusal; retains prior versions; enforces expected-version optimistic concurrency and draft status after any edit.
**Dependencies:** arctrust.artifact, COMP-001, COMP-002
**Inputs:** workspace bundle path, tier, pinned operator public key
**Outputs:** verified WorkflowDefinition + status(draft|signed) + content_hash, or a fail-closed integrity error

### COMP-006: RunStore (arcstore)
**Responsibility:** Durable Run aggregate on the shared mutable plane: run id, workflow id, version, content hash, status, initiator identity, budget counters, and the ordered path taken (materialized nodes, router choices, loop iterations). Status transitions use the existing conditional-update primitive.
**Dependencies:** arcstore backend
**Inputs:** Run create/transition requests with actor identity
**Outputs:** Run records; conditional-write success or refusal

### COMP-007: TaskStore.create_batch (arcstore)
**Responsibility:** New batch creation of task rows across owners in one backend transaction, used for frontier materialization. Idempotent on the run-and-node identity pair so a crashed runner never double-creates.
**Dependencies:** arcstore.tasks
**Inputs:** list[Task] + idempotency keys
**Outputs:** created tasks, or the existing rows when keys already present

### COMP-008: WorkflowRunner (arcteam)
**Responsibility:** The deterministic progressor. Starts runs, materializes the ready frontier lazily, evaluates routers and loop back-edges, resolves wiring, threads accumulated capability legs, enforces run budget and stall detection, orders cancellation, records the path taken, and rolls node terminal states into the Run. Receives tier at construction. Holds no model and makes no LLM call.
**Dependencies:** COMP-001, COMP-002, COMP-003, COMP-004, COMP-005, COMP-006, COMP-007, COMP-009, COMP-010
**Inputs:** run start requests, a tick, tier, runner identity
**Outputs:** task rows written, Run transitions, narration messages, audit events

### COMP-009: RunnerHost (arcgateway)
**Responsibility:** Constructs and owns the runner's lifecycle inside the fleet service bootstrap on the agent side, enforcing a single active runner instance. Gains arcstore and arcteam dependencies. The dashboard is never required for execution.
**Dependencies:** COMP-008, COMP-011
**Inputs:** service startup with resolved config and tier
**Outputs:** a running singleton runner task; refusal to start a second instance

> **Handoff invariant (D-538).** The task-row write that names the next node's owner *is* the handoff. `COMP-010` narration and any wake-signal DM are one-way and carry no work; a run must reach completion with every outbound message dropped. Rationale: a task forces action, retries, keeps history, and names exactly one executor; a message does none of these.

### COMP-010: RunNarrator and channel binding (arcteam)
**Responsibility:** Binds a workflow to its group channel and posts narration-class messages for node start, completion, handoff, gate waiting, gate resolution, and run outcome. Narration is recorded and displayed but never activates an agent.
**Dependencies:** arcteam.messenger, COMP-006
**Inputs:** run transition events
**Outputs:** signed narration messages on the bound channel

### COMP-011: RunnerIdentity (arcteam)
**Responsibility:** Resolves the runner's own actor identity from the on-disk operator key path without generating one, distinct from any agent identity and from the shared dashboard operator identity. Stamps every Run and task row the runner creates.
**Dependencies:** arctrust
**Inputs:** key path
**Outputs:** runner actor DID; fail-closed when absent

### COMP-012: Workflow builder tools (arcagent module)
**Responsibility:** The thin agent-facing surface: create, add/edit/remove node, set trigger, set channel, run, cancel, list, inspect, runs, run status. Every mutating tool uses an explicit field allowlist, owner gating, quota checks before validation work, and normalization of inline free text; validates the whole graph in memory, then checks cycles, then writes atomically. Always produces drafts.
**Dependencies:** COMP-001, COMP-002, COMP-005, COMP-008
**Inputs:** typed tool arguments from an agent
**Outputs:** JSON result or a typed error list with node id, field, observed value, admissible alternatives

### COMP-013: workflow-builder skill (arcagent builtins)
**Responsibility:** The authoring knowledge an agent loads before building: what to elicit from conversation, which node kind fits which work, the named coordination patterns, and the anti-patterns to refuse. Follows the repository's seven-section skill format.
**Dependencies:** COMP-012
**Inputs:** activation by the builder tools
**Outputs:** prompt content guiding elicitation and node-kind choice

### COMP-014: NodeExecutionAdapter (arcagent tasks module)
**Responsibility:** Where typed handoff actually fires: injects upstream outputs and node instructions into the node run through the assemble-prompt seam, activates a declared skill deterministically, passes the declared strategy list to the loop, validates the node's output against its schema on completion, enforces declared artifact existence, and carries the per-attempt idempotency key into tool dispatch.
**Dependencies:** COMP-004, COMP-007, arcagent.modules.tasks
**Inputs:** a claimed workflow node task row
**Outputs:** validated node output persisted, or a retryable failure naming the missing artifact or schema violation

### COMP-015: CapabilityLegThreader (arcagent tasks module)
**Responsibility:** Carries the run's accumulated lethal-trifecta legs into each node's policy context so a fresh per-node session cannot reset the accumulation, and bounds the accumulated set.
**Dependencies:** COMP-006, arctrust.policy
**Inputs:** run id + node dispatch
**Outputs:** policy context carrying the accumulated legs

### COMP-016: ActivationApproval (arcteam + arctrust)
**Responsibility:** Computes the union of capability legs a definition's nodes can touch and requires an operator-signed grant at first activation and on any widening edit, reusing the existing human-gate and approval-grant machinery.
**Dependencies:** COMP-002, arctrust.policy
**Inputs:** WorkflowDefinition + tier
**Outputs:** activation allowed, or a pending approval bound to the definition hash

### COMP-017: Typed schedule action (arcagent scheduler)
**Responsibility:** Extends the schedule entry with a typed action so a trigger dispatches a workflow run directly rather than firing free text at the loop. Reuses the existing circuit breaker, active-hours gating, and in-flight dedup; overlapping firings skip.
**Dependencies:** COMP-008, arcagent.modules.scheduler
**Inputs:** a due schedule entry with a workflow action
**Outputs:** a started run, a skip when one is in flight, or a breaker-driven disable

### COMP-018: Gate resolution surface (arcui + arccli)
**Responsibility:** Control-plane-only gate resolution: an answerable card rendered in the bound channel and the existing review routes, recording the deciding human's authenticated identity and the chosen outcome (fail the run, or return for revision with notes). No agent-callable tool resolves a gate.
**Dependencies:** COMP-006, COMP-010, arcstore.tasks review gates
**Inputs:** an operator decision on a review-status node task
**Outputs:** an audited resolution and the resulting frontier advance or run failure

### COMP-019: Operator signing and workflow CLI (arccli)
**Responsibility:** Out-of-band sign and verify plus list, show, run, and cancel. The operator signing key is resolved here and never enters an agent process.
**Dependencies:** COMP-005, COMP-008
**Inputs:** operator command + bundle path
**Outputs:** a written signature sidecar, a verification result, or a started run

### COMP-020: Workflow management surface (arcui web)
**Responsibility:** The dashboard as a full peer authoring surface, not a viewer: list workflows with status and last run; create a new workflow; view its graph, version history with signer and reason, and run history; edit nodes, edges, trigger, and channel binding; archive a workflow; and watch a live run's per-node status with untaken branches and loop iterations sourced from the Run record's path taken. Every mutation posts through COMP-023 to the shared control plane (COMP-021) and is subject to the identical validator and draft lifecycle as the conversational and file surfaces. The dashboard is a viewer and an initiator: it holds no operational work, has no privileged path, and cannot sign.
**Dependencies:** COMP-023, COMP-006
**Inputs:** operator interactions; workflow id, version, run id
**Outputs:** rendered graph, editor state, and per-node run status; node click opens the existing per-run timeline

### COMP-021: WorkflowControlPlane (arcteam)
**Responsibility:** The single shared operation set for every workflow mutation and initiation — create, edit, archive, unarchive, run, cancel — owning all validation, versioning, draft lifecycle, and audit emission in one place. Every caller invokes these same operations: the agent's builder tools, the operator command line, and the dashboard. There is exactly one implementation of what a workflow edit means, so the three surfaces cannot drift.
**Dependencies:** COMP-002, COMP-005, COMP-006, COMP-008, COMP-022
**Inputs:** a typed operation request plus the acting identity
**Outputs:** the resulting definition or run, or a typed error list; an audit event per operation

### COMP-023: Workflow routes (arcui server) — thin initiator
**Responsibility:** A delegating adapter and nothing more. It authenticates the operator, translates HTTP to a control-plane operation, and returns the result. It holds no operational work: no validation logic, no versioning rules, no sequencing, no signing, no node execution, and no gate resolution of its own. The dashboard initiates real work through the real mechanism; if these routes were deleted the same operations would remain fully reachable from the command line.
**Dependencies:** COMP-021
**Inputs:** HTTP requests from an authenticated operator
**Outputs:** the control plane's result verbatim, plus the boundary audit record already standard for dashboard mutations; refusal for non-operators

### COMP-022: Definition archival (arcteam)
**Responsibility:** Archive rather than erase. Archiving hides a workflow from the active list, refuses new runs and disables its trigger, and leaves the bundle, every retained version, and all run history intact so past runs stay renderable and the audit chain stays whole. Unarchive restores it as a draft. A hard purge is a separate operator-only action that refuses while any run still references the definition and, when forced, records in the audit chain that history for that workflow is henceforth unrenderable.
**Dependencies:** COMP-005, COMP-006
**Inputs:** archive/unarchive/purge request with actor identity
**Outputs:** status transition and audit event; refusal when a purge would orphan live runs


## Data Model

Two persistence surfaces. (1) The definition bundle in the owning agent's workspace: `workflows/<id>/workflow.toml` plus `schemas/`, `prompts/`, `scripts/`, each with a detached `.arcsig` sidecar, and `versions/<n>.toml` retaining prior revisions. Workspace is the source of truth; the store only indexes id, current version, content hash, status, and channel binding. Written by direct filesystem I/O, never through model-facing file tools. (2) The shared mutable plane in arcstore: a new `runs` collection (its own collection, because the operational spool's kind set is closed and carries no run id) and existing `tasks` rows extended additively with workflow, node id, and run identity in metadata. Node ids are immutable once signed; a rename is delete plus add.

## External Integrations

No external services. Internal seams only: `arcstore` task and run persistence; `arcteam.messenger` for narration and signed handoff; `arctrust` for signing, policy evaluation, approval grants, and audit; the scheduler for typed triggers; the agent loop for node execution. Companion work, separately specified: a `subagents` loop strategy whose shape lives in the loop layer and whose spawn and messaging execution is injected from the agent layer, preserving the rule that a strategy never sees the graph and the loop never imports the agent package.

## Traceability

| Requirement | Components |
|---|---|
| REQ-217 | COMP-001, COMP-005 |
| REQ-218 | COMP-001, COMP-008 |
| REQ-219 | COMP-002, COMP-003 |
| REQ-220 | COMP-002, COMP-008 |
| REQ-221 | COMP-012 |
| REQ-222 | COMP-002, COMP-012 |
| REQ-223 | COMP-005, COMP-012 |
| REQ-224 | COMP-019 |
| REQ-225 | COMP-005 |
| REQ-226 | COMP-005 |
| REQ-227 | COMP-005, COMP-008 |
| REQ-228 | COMP-006, COMP-008 |
| REQ-229 | COMP-007, COMP-008 |
| REQ-230 | COMP-008, COMP-009 |
| REQ-231 | COMP-009 |
| REQ-232 | COMP-011 |
| REQ-233 | COMP-008, COMP-014 |
| REQ-234 | COMP-007, COMP-008 |
| REQ-235 | COMP-006, COMP-008 |
| REQ-236 | COMP-006, COMP-008 |
| REQ-237 | COMP-014 |
| REQ-238 | COMP-014 |
| REQ-239 | COMP-004, COMP-014 |
| REQ-240 | COMP-015 |
| REQ-241 | COMP-016 |
| REQ-242 | COMP-014 |
| REQ-243 | COMP-014 |
| REQ-244 | COMP-010 |
| REQ-245 | COMP-010, COMP-018 |
| REQ-246 | COMP-018 |
| REQ-247 | COMP-018 |
| REQ-248 | COMP-005, COMP-012 |
| REQ-249 | COMP-017 |
| REQ-250 | COMP-020 |
| REQ-251 | COMP-007, COMP-008, COMP-010 |
| REQ-252 | COMP-020, COMP-021, COMP-023 |
| REQ-253 | COMP-021, COMP-023 |
| REQ-254 | COMP-021, COMP-023 |
| REQ-255 | COMP-022 |
| REQ-256 | COMP-022 |
| REQ-257 | COMP-019, COMP-021, COMP-012 |

## Alternatives Considered

Considered extending the planning module's DAG engine (rejected: its plans are ephemeral, single-agent, and self-decomposed; ArcFlow definitions are operator-auditable artifacts and multi-agent). Considered a third bespoke executor (rejected: the task layer already owns atomic claims, dependency gating, retries, dead-letter, and review gates — a fork would duplicate battle-tested code). Considered a new top-level package (rejected: install and deploy surface, with no second harness needing the engine yet; the frequently cited architecture guard would not actually have fired). Considered hosting the runner in the dashboard process, which is the only host holding both halves today (rejected by the operator: the dashboard is a watcher and initiator, and execution must never require it). Considered CEL for predicates (rejected: the only pure-Python implementation is beta, and an operator signing a definition would have to audit a full grammar rather than four node types). Considered instantiating the whole graph upfront with a skipped status per untaken node (rejected: an untaken branch needs no record; the path taken on the Run is the honest trace, and lazy materialization is required for loops regardless). Considered free graph cycles as in general agent-graph frameworks (rejected: declared bounded back-edges preserve auditability and budget control). Considered the newer scheduler module for the typed trigger (rejected: it is an unwired skeleton with no store, no CRUD, and a no-op handler).

## Risks and Mitigations

Producers-unwired is this repository's recurring failure: correct predicates shipped with dead activating wiring. Four seams here are the likely victims and each needs an end-to-end test through the real path — the typed schedule trigger, output-schema validation firing on node completion, the signed-definition refusal at enterprise and federal tier, and accumulated capability legs reaching the policy context beyond the first node. Second: the single-runner design is safe only while one instance exists; a lease alone does not make multiple runners safe, so relaxing this later requires fencing tokens. Third: the layer model document omits the store package entirely and must be updated in the same change or it is wrong on merge. Fourth: the agent package declares neither the store nor the team package today and treats the latter as optional; a scaffold-enabled module makes that dependency real. Fifth: per-run capability-leg accumulation is an unbounded collection and must be bounded and monitored. Sixth: the runner adds another poller against the shared store in the same process as existing dispatch loops, so its queries must be scoped rather than list-then-filter.

## Open Questions

- Channel binding granularity: one channel per workflow with a thread per run, or a channel per run?
- Dashboard editor scope: full graph editing, or node-property editing over a rendered graph first?
- Which event trigger lands first after v1 — message, webhook, or task-created?
- Dependency posture: declare the store and team packages in the agent package metadata, or keep lazy imports with graceful degradation?
