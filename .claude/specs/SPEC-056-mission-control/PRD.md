# SPEC-056 — Mission Control (multi-agent task system) · PRD

**Status:** DRAFT · **Created:** 2026-07-12 · **Roadmap:** ORCH-2 ([[project_orchestration_roadmap]])
**Source:** brainstorm `.claude/brainstorms/2026-07-12-mission-control-task-system.md` (decisions resolved §5)
**Packages:** arcstore (durable directory) · arcagent (`tasks` module) · arcteam (assignment envelope) · arcui (kanban + routes)

## 1. Overview

A shared task system that turns arc from "agents that message" into "agents that
coordinate work." Every agent owns a task list in its own harness; tasks are
created by an agent for itself or **assigned** by another; the human watches and
steers a team-wide **kanban** in arcui. The task list — not chat — becomes arc's
coordination channel (Anthropic's #1 multi-agent finding, [[reference_claude_multiagent_patterns]]).

## 2. Goals

- G1 — An agent can create a task for itself, work it, and mark it done/failed.
- G2 — An agent can assign a task to a teammate; the teammate is notified and owns it.
- G3 — Each task has **exactly one owner**; no two agents contend for the same task.
- G4 — arcui shows each agent's tasks in its detail view and the whole team's tasks
  as a live kanban.
- G5 — A human can edit a task **at rest** and **steer** an in-flight task by
  messaging its owner — never editing work already in progress.
- G6 — Everything is durable, signed where it crosses agents, and audited.

## 3. Users

- **Agent** — creates/works/assigns/claims tasks via tools inside its loop.
- **Operator (human)** — views the kanban, creates/edits/reassigns at-rest tasks,
  steers in-flight ones by messaging the owner.
- **Viewer (human)** — read-only kanban + per-agent lists.

## 4. Functional requirements

### 4.0 Absorbed prerequisites (the deepen's two dependency risks — built FIRST)

- **FR-0a — arcstore mutable directory plane (completes the SPEC-032 risk).** arcstore
  today is insert-once telemetry only; this spec builds the SPEC-032 mutable plane —
  a durable `mutable_records(collection, key, value, updated_at, PK(collection,key))`
  table + backend + an **atomic conditional-write** seam (the single-owner claim
  primitive) — on which the tasks collection (FR-1..) sits. The broader SPEC-032
  migration of entities/teams/channels onto this plane stays in SPEC-032; SPEC-056
  delivers the plane + the tasks collection.
- **FR-0b — mention-scoped activation (completes the SPEC-055 risk).** Implement
  SPEC-055's `_should_activate` gate in the arcagent messaging inbox so a channel
  @mention wakes only the mentioned agent (DM + critical always wake; un-mentioned
  broadcast wakes all). Required for FR-3 assignee-wake and the fleet-wide token-cost
  fix. Full spec: `.claude/specs/SPEC-055-mention-scoped-activation/`.

### 4.1 Task system

- **FR-1 Create** — an agent creates a task (`title`, `description`, `priority`,
  optional `owner_did`, optional `blocked_by`) via a `create_task` tool. Unowned →
  team backlog; owned → the owner's list.
- **FR-2 Work** — an agent transitions its own task `todo → in_progress → review →
  done` (or `failed`) via tools; `resolution` captured on terminal states.
- **FR-3 Assign** — an agent assigns/reassigns an at-rest task to a teammate
  (`assign_task`). The assignee receives a signed `task.assigned` notification
  (arcteam) that wakes it (SPEC-055: it is the addressed owner).
- **FR-4 Claim (pull)** — an agent requests the next unblocked, unowned task by
  priority (`claim_task`). Claim is **atomic** (single owner, no double-grab) and
  returns a **self-describing reason**: `assigned | continue_current | at_capacity
  | no_tasks_available`.
- **FR-5 Single owner / concurrency** — a task has one `owner_did`; an agent holds
  **one `in_progress` task at a time**; a claim/start of a NEW *independent* task
  while active → `continue_current`. **Dependency exception:** an agent may OWN a
  task's dependency chain (its `blocked_by` targets / decomposed sub-tasks) and work
  it one-at-a-time in dependency order — a dependency of the current work is not a
  competing second task. The cap is on `in_progress`, not on ownership.
- **FR-6 Team read** — `/api/team/tasks` returns every agent's tasks (kanban);
  `/api/agents/{id}/tasks` returns one agent's (detail view). *(Read endpoints
  already exist; they must serve arcstore-backed data.)*
- **FR-7 Human at-rest mutation** — operator-gated, audited arcui routes to create,
  edit (title/description/priority), reassign `owner_did`, and reprioritize a task
  **only when it is not `in_progress`**.
- **FR-8 Steer-in-flight** — the arcui path to affect an `in_progress` task is
  "message the owner" (existing team messaging), not a task edit. UI surfaces this
  explicitly (a "steer owner" action, not an "edit" form, for in-progress cards).
- **FR-9 Dependency ordering + capacity (v1-functional)** — `blocked_by:
  list[task_id]` is on the model AND enforced in v1: a task cannot be `started`
  until its `blocked_by` deps are `done` (ordering), and a task's own dependency
  chain is exempt from the FR-5 one-active cap. The heavier dependency-graph
  *visualization / blocking board UI* is **v2**; the claim/start/cap logic is
  dependency-aware in v1.
- **FR-10 Live updates** — kanban + detail lists update live over the existing
  arcui `/ws` off arcstore (no new realtime infra; SPEC-026 pull-from-arcstore).

### 4.2 Observability, CLI, decomposition (2026-07-12 additions)

- **FR-11 Per-task run + cost link** — a task carries `run_id` (set when a run picks
  it up). arcui links the card → the existing run/trace view; cost + tokens + owning
  agent come from that run's `llm_calls` (already joined on `request_id == run_id` by
  `observe.timeline`). No new accounting — reuse arcllm/arcstore cost data.
- **FR-12 Activity timeline** — the task detail surfaces its audit-event history
  (created / assigned / started / completed / failed, with actor + timestamp) as a
  timeline, read from the audit stream filtered by `target == task_id`.
- **FR-13 Structured task output** — on completion a task records a structured
  `output` (result summary + artifact links), not just a free-text `resolution`
  (mirrors Claude's `TaskOutput`). Set via `complete_task` / a `set_task_output` tool.
- **FR-14 CLI surface** — `arc task` commands to create, list, edit, assign,
  complete, and **steer ("talk to") the owner** — the same operations as the tools +
  arcui, not UI-only. Human writes are operator-gated + audited; "talk" routes an
  arcteam message to the owner (same edit-at-rest / steer-in-flight rule as FR-7/FR-8).
- **FR-15 Decompose to sub-tasks** — a task can be decomposed into sub-tasks (linked
  via `parent_id` + `blocked_by`), run by the **same agent** (its dependency chain,
  FR-5 exemption) OR **assigned to spawned child agents** (arc spawn/delegation). Via
  a `decompose_task` tool.
- **FR-16 Board filters + metrics + counts** — the tasks view supports filters
  (owner / priority / status / tag), a metrics row up top (e.g. in-progress,
  done-today, avg time-to-done), and counts below (tasks, inbox, blocked, etc).

## 5. Non-functional requirements

- **NFR-1 Concern separation** — durable truth = arcstore; per-agent tools/loop =
  arcagent; cross-agent transport = arcteam; view = arcui. No mixing.
- **NFR-2 Atomicity** — claim/assign are atomic conditional writes; no lost updates,
  no double ownership under concurrency.
- **NFR-3 Audit** — every create/transition/assign/claim/human-mutation emits an
  audit event (actor DID). Cross-agent assignment is Ed25519-signed.
- **NFR-4 Security** — human mutations operator-gated; agents can only mutate tasks
  they own or (for assign/create) address a valid teammate DID; input sanitized
  (LLM01/ASI06). No task can be silently reassigned away from an active owner.
- **NFR-5 Quality gates** — ruff, mypy --strict, ≥80% coverage, tsc clean.

## 6. Out of scope (this spec)

- Coordinator/lead that decomposes→delegates→synthesizes (**ORCH-3, deferred**).
- Completion **verification** logic (the `review` state ships as an inert hook).
- Dependency-graph **visualization / blocking board UI** (v2; ordering + cap logic ship in v1 per FR-9).
- Task→run **auto-drive** (arrives with the coordinator; v1 is pull/tool-driven).
- Multi-project namespacing / ticket refs (mission-control has it; YAGNI for arc v1).

## 7. Success criteria

- An agent creates a task, another agent is assigned one and is woken to work it,
  and both appear on the live kanban with correct owners — verified live on olivia.
- Two agents claiming simultaneously never double-own a task (atomicity test).
- A human edits an at-rest task and reprioritizes it from the board; attempting to
  edit an in-progress task offers "steer owner" instead — verified.
- Quality gates green across arcstore/arcagent/arcteam/arcui + tsc.
