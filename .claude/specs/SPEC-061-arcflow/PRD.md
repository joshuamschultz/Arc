# Product Requirements Document: ArcFlow — Named, Signed, Conversationally-Authored Workflows

## Context References

- **Personas:** [.claude/steering/product.md#user-personas](../../steering/product.md#user-personas)
- **Constraints:** [.claude/steering/product.md#business-constraints](../../steering/product.md#business-constraints)
- **Metrics Framework:** [.claude/steering/product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework)
- **Current Phase:** [.claude/steering/roadmap.md#current-phase](../../steering/roadmap.md#current-phase)

## Product Overview

### Vision
A named, semi-permanent, signed graph of nodes that an agent authors from conversation, a human edits in an IDE, and an operator manages in arcui — run repeatedly on demand or on a schedule, executed by a deterministic fleet runner over the existing task DAG, and auditable per version, per run, and per node.

### Problem Statement
Arc can reason its way through an ad-hoc goal, but it cannot run the same business process the same way twice. Stage order lives in orchestrator code or, worse, in prompt wording; dependencies between steps exist only inside a context window; a plan can be silently rewritten mid-run with no record of the original. There is no artifact an operator can read, diff, sign, or hold a process to.

### Value Proposition
Turns repeatable business processes (onboarding, sales pipelines, report generation, intake-and-route) into governable artifacts. The topology becomes data the operator signs rather than behavior that emerges from a prompt — which is simultaneously what makes the process reliable, what makes it auditable for FedRAMP/NIST, and what gives the app-store factory a small checkable generation target instead of asking a model to write correct orchestration code.

## Personas

See `.claude/steering/product.md#user-personas`. Primary: the operator who owns a business process and signs its definition. Secondary: the agent that authors and runs it; the reviewer who answers gates in chat.

## User Stories

- **US-1**: As an operator, I want to describe a repeatable process in conversation and have my agent build it into a named workflow, so that the process is captured as an artifact instead of living in one person's head..
- **US-2**: As an operator, I want every workflow definition to be signed by me before it can run at enterprise or federal tier, so that a prompt injection can never author and bless an exfiltration pipeline..
- **US-3**: As an operator, I want to run a workflow on demand or on a schedule and have it execute identically every time, so that the process is reliable rather than re-improvised per run..
- **US-4**: As an agent, I want to hand work to the right teammate agent at each node with typed inputs and outputs, so that downstream nodes consume a contract instead of re-deriving context from prose..
- **US-5**: As a reviewer, I want to answer a workflow's approval gate in the same channel where the work is narrated, so that I can decide in context without hunting a separate queue..
- **US-6**: As an operator, I want to change a workflow by talking about it, in an IDE, or in the dashboard, so that the process evolves without a developer and without three divergent representations..
- **US-7**: As an auditor, I want to see which version ran, the exact path taken, who signed it, and who approved each gate, so that the run is defensible under NIST 800-53 AU controls..

## Functional Requirements

- **REQ-217** (story US-1, Must): The system SHALL represent a workflow as a single canonical signed `workflow.toml` artifact in the owning agent's workspace that simultaneously serves as the executable definition, the render source for every viewer, and the diffable audit record.
- **REQ-218** (story US-1, Must): The system SHALL support five node kinds — `agent`, `tool`, `script`, `router`, and `gate` — each declaring its executing agent, with `needs` edges expressing dependencies.
- **REQ-219** (story US-1, Must): WHEN a workflow definition is validated THEN the system SHALL reject dangling `needs`, undeclared cycles, unknown agents, tools, skills or scripts, unresolvable schema references, unparseable `when` predicates, loops missing `max_iterations`, and statically unsatisfiable `$nodes.*.output.*` references.
- **REQ-220** (story US-1, Must): The system SHALL treat a node's `needs` as satisfied when each named upstream node reaches `done` or `skipped`, SHALL support `join = "all" | "any"` defaulting to `all`, and SHALL statically reject a node whose `needs` span mutually exclusive routes of one router unless it declares `join = "any"`.
- **REQ-221** (story US-1, Must): WHERE an agent authors or edits a workflow THEN the system SHALL expose only validated builder tools that emit into the canonical schema, and SHALL NOT accept a model-generated definition document directly.
- **REQ-222** (story US-1, Must): WHEN a builder tool rejects a graph THEN the system SHALL return a typed error list carrying node id, field path, observed value, and admissible alternatives, and SHALL bound self-repair to at most three attempts before surfacing a clarification request to the human.
- **REQ-223** (story US-2, Must): The system SHALL produce every agent-authored, IDE-authored, and dashboard-authored definition as an unsigned draft, and SHALL NOT provide any path by which successful validation confers signed status.
- **REQ-224** (story US-2, Must): The system SHALL sign a workflow only through an out-of-band operator command whose signing key never enters an agent process.
- **REQ-225** (story US-2, Must): IF a workflow definition is unsigned or signed by a key other than the pinned operator key THEN the system SHALL refuse to execute it at enterprise and federal tiers, and SHALL execute it at personal tier only while emitting an audit warning.
- **REQ-226** (story US-2, Must): The system SHALL compute the content hash over a canonical serialization of the parsed definition together with a sorted manifest of every referenced schema, prompt, and script file, and SHALL NOT hash raw file bytes.
- **REQ-227** (story US-2, Must): WHEN a referenced file's hash does not match the signed manifest at run start or at node dispatch THEN the system SHALL fail the run closed rather than execute a hybrid of two versions.
- **REQ-228** (story US-3, Must): WHEN a workflow run is started THEN the system SHALL create a durable Run record carrying run id, workflow id, version, content hash, status, initiator identity, budget, and the path taken.
- **REQ-229** (story US-3, Must): The system SHALL materialize a node into a task row only when that node becomes reachable, SHALL make materialization idempotent on the run and node identity pair, and SHALL NOT create task rows for branches the run never takes.
- **REQ-230** (story US-3, Must): The system SHALL progress a run through deterministic runner code hosted on the agent side of the fleet service, and SHALL NOT require the dashboard process to be running for a workflow to execute.
- **REQ-231** (story US-3, Must): WHILE more than one runner instance could exist the system SHALL enforce a single active runner so that two instances never advance the same run frontier.
- **REQ-232** (story US-3, Must): The system SHALL execute each node under a distinct runner identity that is neither an agent identity nor the shared dashboard operator identity.
- **REQ-233** (story US-3, Must): WHEN a node completes successfully THEN the system SHALL record its validated output once and SHALL NOT re-execute that node on any subsequent resume or replay.
- **REQ-234** (story US-3, Must): IF the runner restarts mid-materialization THEN the system SHALL re-derive already-materialized nodes from existing task rows rather than creating duplicates.
- **REQ-235** (story US-3, Must): WHEN a run is cancelled THEN the system SHALL mark the Run record cancelled before fanning out node-level cancellation so that a node completing during the sweep cannot extend the frontier.
- **REQ-236** (story US-3, Must): The system SHALL enforce a run-level token and wall-clock budget across all nodes of a run using reserve-then-settle accounting, and SHALL terminate a run that exhausts it.
- **REQ-237** (story US-4, Must): The system SHALL validate a node's output against its declared `output_schema` before that output is visible downstream, and SHALL treat a schema failure as a retryable node failure rather than passing the value forward.
- **REQ-238** (story US-4, Must): WHERE a node declares required artifacts THEN the system SHALL treat the node as complete only when those files exist, and SHALL name the specific tool that produces each missing artifact in the retry message.
- **REQ-239** (story US-4, Must): The system SHALL resolve `$nodes.*.output.*` and `$input.*` references by binding typed values into tool arguments or prompt sections, and SHALL NOT interpolate upstream output textually into a command or prompt string.
- **REQ-240** (story US-4, Must): The system SHALL accumulate lethal-trifecta capability legs across every node of a run and present the accumulated set to the policy pipeline on each node's tool calls.
- **REQ-241** (story US-4, Must): IF a workflow definition's nodes jointly span a forbidden capability composition THEN the system SHALL require an operator-signed approval at first activation and on any edit that widens the composition.
- **REQ-242** (story US-4, Must): The system SHALL carry a per-attempt idempotency key into tool dispatch so that a retried node cannot repeat an external side effect.
- **REQ-243** (story US-4, Should): WHERE an agent node declares a strategy list THEN the system SHALL pass it to the loop as the allowed strategy set, and WHERE no strategy is declared the system SHALL pin the reactive strategy.
- **REQ-244** (story US-5, Should): The system SHALL bind each workflow to a group channel whose members are its participating agents and the involved people, and SHALL narrate every node transition, handoff, gate, and run outcome there without waking any agent.
- **REQ-245** (story US-5, Should): WHEN a gate node is reached THEN the system SHALL surface an answerable decision in the bound channel and SHALL record the resolution against the authenticated identity of the deciding human.
- **REQ-246** (story US-5, Must): The system SHALL NOT expose any agent-callable tool that resolves a gate, and SHALL accept gate resolution only through the control plane.
- **REQ-247** (story US-5, Should): WHEN a reviewer rejects a gate THEN the system SHALL let that reviewer choose between failing the run and returning it for revision with notes, and SHALL audit which was chosen.
- **REQ-248** (story US-6, Should): WHEN a workflow is edited through any surface THEN the system SHALL require the editor's expected version, SHALL reject a stale edit rather than merge it, SHALL increment the version, and SHALL return the definition to draft status.
- **REQ-249** (story US-3, Should): WHERE a workflow declares a trigger THEN the system SHALL dispatch the run through a typed scheduled action rather than a free-text prompt, and SHALL skip a firing whose prior run is still in progress.
- **REQ-250** (story US-7, Could): The system SHALL render a workflow definition and each run's live per-node status as a graph in the dashboard, sourcing untaken branches and loop iterations from the Run record's path taken.
- **REQ-251** (story US-4, Must): The system SHALL carry every handoff between nodes as a task-row write that names the receiving agent as owner, SHALL NOT carry work in any message body, and SHALL advance a run to completion even when every outbound message is dropped.
- **REQ-252** (story US-6, Must): The system SHALL let an operator create, view, edit, archive, and run a workflow entirely from the dashboard, without editing a file or issuing a command.
- **REQ-257** (story US-6, Must): The system SHALL make every workflow capability — create, view, edit, archive, purge, run, and cancel — reachable from the command line and from conversation with an agent, and SHALL remain fully functional with the dashboard uninstalled.
- **REQ-253** (story US-6, Must): WHEN the dashboard submits a create or edit THEN the system SHALL apply the identical validator, versioning, and draft lifecycle used by the conversational and file surfaces, and SHALL return the same typed error list.
- **REQ-254** (story US-6, Must): The system SHALL NOT provide any dashboard path that signs a definition, and SHALL restrict every workflow mutation route to an authenticated operator with an audit event recording that person's identity and the outcome.
- **REQ-255** (story US-6, Should): WHEN an operator deletes a workflow THEN the system SHALL archive it — hiding it from the active list, disabling its trigger, and refusing new runs — WHILE retaining the bundle, every prior version, and all run history so past runs stay renderable.
- **REQ-256** (story US-7, Should): IF an operator requests a permanent purge of a workflow THEN the system SHALL refuse while any run still references it, and WHERE forced SHALL record in the audit chain that history for that workflow is henceforth unrenderable.

## MoSCoW Priorities

| Priority | Requirements |
|---|---|
| Must | REQ-217, REQ-218, REQ-219, REQ-220, REQ-221, REQ-222, REQ-223, REQ-224, REQ-225, REQ-226, REQ-227, REQ-228, REQ-229, REQ-230, REQ-231, REQ-232, REQ-233, REQ-234, REQ-235, REQ-236, REQ-237, REQ-238, REQ-239, REQ-240, REQ-241, REQ-242, REQ-246, REQ-251, REQ-252, REQ-253, REQ-254, REQ-257 |
| Should | REQ-243, REQ-244, REQ-245, REQ-247, REQ-248, REQ-249, REQ-255, REQ-256 |
| Could | REQ-250 |
| Won't | _(none)_ |

## Success Metrics

Framework: see `.claude/steering/product.md#success-metrics-framework`. Targets for this feature: an operator describes a three-node multi-agent workflow in conversation, signs it, and runs it twice with identical node sequences; 100% of runs reconstruct version, path taken, signer, and gate approvers from durable signed records; zero paths exist by which validation success confers signed status (proven by test); accumulated capability legs reach the policy pipeline on every node beyond the first (proven by end-to-end test); a definition of up to 200 nodes validates in under one second.

## Risks and Constraints

Producers-unwired is this repo's recurring failure mode (SPEC-034/035/037/038/040/043/044/056): correct predicates shipped with dead activating wiring. The four highest-risk seams here are the scheduled trigger, output-schema validation firing on node completion, the signed-definition gate refusing at enterprise and federal tier, and accumulated capability legs reaching the policy context — each requires an end-to-end test through the real path, not a unit test of the predicate. Second risk: the fleet-singleton runner is a deliberate scalability ceiling; relaxing it later requires fencing tokens, not just a lease. Third risk: `TaskStore.create_batch` and the Run store are new persistence work, not existing machinery. Fourth risk: per-run capability-leg accumulation is an unbounded collection and must be bounded and monitored (SPEC-009 lesson). Mitigations are tracked as tasks in PLAN.md.

## Open Questions

- Channel binding granularity: one channel per workflow with a thread per run, or a channel per run?
- Dashboard editor scope: full graph editing, or node-property editing over a rendered graph first?
- Which event trigger lands first after v1 — message, webhook, or task-created?
- Dependency posture: declare arcstore and arcteam in arcagent's project metadata, or keep lazy imports with graceful degradation?
