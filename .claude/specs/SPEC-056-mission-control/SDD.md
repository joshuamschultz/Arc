# SPEC-056 — Mission Control · SDD (design)

Traces PRD FR/NFR. Mirrors the **scheduler** module (`arcagent/modules/scheduler/`)
as the arcagent template and the **SPEC-032 arcstore directory** as the durable store.

## 0. Absorbed prerequisites (built first — the deepen's two dependency risks)

### 0a. arcstore mutable directory plane (SPEC-032 slice) — FR-0a
arcstore's spool plane is insert-once (base.py `OPERATIONAL_TABLES`); add the SPEC-032
mutable plane to the SQLite backend (WAL + `busy_timeout` + `BEGIN IMMEDIATE`):
`mutable_records(collection TEXT, key TEXT, value JSON, updated_at TEXT,
PRIMARY KEY(collection,key))` with `read/write/delete/query`, plus the one primitive
the claim needs — an **atomic conditional write** `update_if(collection, key, set,
where) -> bool` returning `rowcount > 0`, copied from the `pairing.py:788-794`
pattern. Tasks (§2) are one `collection` on this plane. Deliver ONLY the plane + the
tasks collection here; the entities/teams/channels migration remains SPEC-032.

### 0a-1. Scale ceiling / known limitation — single-file SQLite

The tasks feature has every agent, arcui, and the CLI open a writer on **one**
`store/arcui.db` (the `mutable_records` plane from §0a). `SqliteBackend`
(`arcstore/backends/sqlite.py:1-11`) already documents the consequence: a
shared file across instances produces `SQLITE_BUSY` storms above ~2-3
concurrent writers, because SQLite is fundamentally single-writer per file.

**Current posture:** fine for single-host / small-fleet operation. WAL +
`busy_timeout` + `BEGIN IMMEDIATE` (the atomic `update_if` primitive) let
concurrent writers *serialize* safely — nobody corrupts state, nobody loses a
claim — but they don't make the file *scale*. As writer count climbs toward
the target of 1,000s of agents, `busy_timeout` arbitration among many
processes becomes contention, not concurrency, and is itself a latent DoS
surface (ASI08).

**Documented path to raise the ceiling (not built now):** replace the local
file with a networked backend behind the same `StorageBackend` /
`MutableTaskBackend` Protocol (`arcstore/backends/base.py`,
`arcstore/tasks.py`) — e.g. a real multi-writer database — or, cheaper,
elect a single write-owner process that all other writers proxy through.
This is the same tradeoff SPEC-032's research already named and deferred:
*"the hardened pattern is a single elected write-owner (others proxy) —
`busy_timeout` arbitration among many processes is itself a DoS surface...
document write-owner as the scaling/hardening path, don't build it now"*
(SPEC-032 SDD:85). SPEC-056 inherits that decision as-is; nothing here
changes it, it just adds three more writer classes to the same file.

### 0b. Mention-scoped activation (SPEC-055) — FR-0b
Implement `_should_activate(msg, identity)` in `modules/messaging/capabilities.py`
`_handle_incoming` per the SPEC-055 README: wake if `priority==critical` OR
`not msg.mentions` (DM/broadcast) OR `identity.did in msg.mentions`; else
**ack-and-ignore** (no `deliver_fn`/`agent_run_fn`, no follow_up). arcagent-only; the
channel stream remains the record for ignored traffic. Ship + deploy + live-verify
(one @mention → 1 run, not 4), closing SPEC-055.

## 1. Architecture / concern map (NFR-1)

```
arcstore   → durable Task directory (source of truth) + atomic claim/assign
arcagent   → `tasks` module: tools the agent calls in its loop (create/work/assign/claim)
arcteam    → signed `task.assigned` envelope → assignee inbox (SPEC-055 wakes owner)
arcui      → read projection (kanban + per-agent list) + operator mutation routes + steer action
```
No layer reaches across: agents never write arcstore rows directly for *other* agents
(they send an arcteam envelope); arcui never mutates through the agent (operator writes
go to arcstore via a guarded route + audit); arcstore holds no agent/loop logic.

## 2. Data model (arcstore) — FR-1, FR-9, NFR-2

New `tasks` table in the arcstore durable directory (alongside entities/teams/channels):

```
Task:
  id: str                      # "{owner_or_team}-{seq}" or uuid; stable
  title: str
  description: str = ""
  status: TaskStatus           # see §4
  priority: Priority           # low | medium | high | critical
  owner_did: str | None        # exactly-one assignee; None = team backlog
  creator_did: str             # who created it (agent DID or operator DID)
  parent_id: str | None        # decomposition parent (FR-15); None = top-level task
  run_id: str | None           # LIVE run → cost/agent via request_id==run_id (FR-11); identity≠liveness
  blocked_by: list[str] = []   # dependency edges; v1-functional (ordering + cap exemption, FR-9)
  tags: list[str] = []
  metadata: dict = {}          # opaque
  output: dict | None          # structured result {summary, artifacts:[...]} (FR-13)
  resolution: str | None       # short why-done/failed
  created_at / updated_at: str # ISO
```
Indexes (only the 3 real queries, per mission-control's minimalism): `status`,
`owner_did`, `created_at`. Pydantic model at the arcstore boundary.

### Atomic claim/assign (NFR-2, FR-4, FR-5, G3)
arcstore exposes conditional writes so ownership can never race:
- `claim_next(agent_did) -> (Task|None, reason)`: in one transaction, if the agent
  already has an `in_progress` task → `(that_task, "continue_current")`; else select
  the highest-priority `todo` with `owner_did IS NULL` **and all `blocked_by` deps
  `done`** (v1 ordering — skip tasks with unmet deps), set `owner_did = agent`,
  `status = in_progress` → `(task, "assigned")`; else `(None, "no_tasks_available")`.
- **Dependency-chain exemption (FR-5):** the one-`in_progress` cap counts *independent*
  work. `start_task(id)` on a task that is a dependency of (or is `blocked_by`-linked
  to) the agent's currently-owned chain is allowed even though it becomes a second
  `in_progress`-adjacent item — the guard is "no NEW *unrelated* in_progress," not
  "one owned task." Enforce by checking whether `id` shares a `blocked_by` chain with
  the agent's active task before applying the cap. `at_capacity` is returned only when
  a genuine independent second task is attempted.
- `assign(task_id, to_did, by_did)`: conditional — rejects if task is `in_progress`
  owned by someone else (NFR-4: no yanking active work); else set `owner_did`,
  audit. Assignment to a teammate is followed by an arcteam notification (§5).
- All writes bump `updated_at` + emit an audit event (NFR-3).

## 3. arcagent `tasks` module (mirror scheduler) — FR-1..FR-5

```
arcagent/modules/tasks/
  models.py        # Task, TaskStatus, Priority, ClaimReason (re-export arcstore model or thin wrapper)
  config.py        # TasksConfig (enabled, max_active=1)
  store.py         # thin client over the arcstore tasks table (read/write/claim)
  capabilities.py  # the tools (below) + audit + input sanitize (LLM01/ASI06)
  _runtime.py      # per-agent state (identity, config, cached handles)
  __init__.py      # module registration (mirror scheduler __init__)
```
**Tools (capabilities.py):**
- `create_task(title, description?, priority?, owner?, blocked_by?)` — owner defaults
  to self; a teammate owner triggers the assignment path (§5).
- `update_task(id, {title?/description?/priority?})` — owner-only, at-rest.
- `start_task(id)` / `complete_task(id, resolution)` / `fail_task(id, resolution)` —
  owner-only status transitions (§4).
- `assign_task(id, to_handle)` — reassign an at-rest task to a teammate (→ §5).
- `claim_task()` — pull the next task; returns the self-describing `reason` (FR-4).
- `list_tasks(scope=self|team, status?)` — read.
- `decompose_task(id, subtasks[])` — create sub-tasks (`parent_id=id`, parent `blocked_by` them); each runs in this agent's chain (FR-5) or is `assign`-ed to a spawned child agent (FR-15).
- `set_task_output(id, output)` — attach the structured result (FR-13); `complete_task` may take it inline.

All tools resolve `@handle → DID` via arcteam's `resolve()`; **audit is emitted
centrally by the tool registry keyed on each tool's `classification`** (`read_only`
vs `state_modifying`) — tools *declare* classification, they don't call `emit`
themselves (deepen correction); all sanitize free-text (NFKC + injection-regex,
mirror scheduler `validate_prompt`) before persistence. Owner identity for
owner-only checks comes from the **messaging-style `_runtime`** (`st.identity`), not
the scheduler template (which has none).

## 4. Status model + transitions — FR-2, FR-9

Enum (richer than board columns; collapse at render — mission-control lesson):
`backlog · todo · in_progress · review · done · failed`, plus a **v1-derived
`blocked`** state when a task has an unmet `blocked_by` (not claimable/startable until
its deps are `done`).

```
create(unowned) → backlog        create(owned) → todo
claim/assign → todo → in_progress (atomic, sets owner + run binding on run start)
in_progress → review             (inert gate — ORCH-3 fills; v1 auto-passes)
review → done                    complete_task
in_progress|review → failed      fail_task
```
- `review` is a **no-op hook point** in v1 (auto-advances) so ORCH-3 verification
  drops in without a schema change (FR: completion gate inert).
- `run_id` is set when a run picks up the task, cleared on terminal state.

## 5. Cross-agent assignment — FR-3 (arcteam)

Assigning to a teammate is two steps, split by concern:
1. **arcstore**: `assign(task_id, to_did, by_did)` writes the owner (durable truth).
2. **arcteam**: send a signed message to the assignee's inbox using a new
   `MsgType.TASK_ASSIGNED = "task_assigned"` enum member (deepen correction — avoid
   the dot and the existing `TASK="task"` collision). The assignee `@handle` goes
   **in the body** (direct `mentions=` is overwritten by `apply_mentions`, which also
   sets `action_required`); body = task id + terse summary only (no-write-down).
   Address it as a **DM to `agent://assignee`** so only the assignee's inbox receives
   it (works even before SPEC-055/0B lands); under 0B the assignee wakes, others ignore.

The assignee's loop, on the delivery, calls `claim_task`/`start_task` for that id
(or the notification carries the id and `start_task` is invoked directly). No agent
writes another agent's store; the envelope is the hand-off signal.

## 6. arcui — FR-6, FR-7, FR-8, FR-10

- **Read (exists, re-point to arcstore):** `/api/team/tasks` (kanban aggregation) +
  `/api/agents/{id}/tasks` (detail) now serve arcstore rows instead of scanning
  `tasks.json`. Keep `TasksResponse`. Per-agent `tasks.json` becomes an optional
  read projection only if needed; arcstore is source of truth.
- **Kanban view (new):** a team Tasks page — columns `backlog/todo/in_progress/
  review/done` (+ failed lane); cards show title, priority, owner handle, blocked
  badge (v2), and a link to the live run (`run_id`) when in progress. Live via the
  existing `/ws` off arcstore (FR-10; SPEC-026 — no parallel push wire).
- **Per-agent list:** in agent-detail, the agent's own tasks (mirror the sessions/
  schedules tabs).
- **Mutation routes (new, operator-gated + audited) — FR-7:** `POST /api/team/tasks`
  (create), `PATCH /api/tasks/{id}` (edit/reassign/reprioritize) — **reject with 409
  if `status == in_progress`** (edit-at-rest only). This is the mutation seam SCHED2
  still lacks; tasks ship with it.
- **Steer-in-flight — FR-8:** an in-progress card shows **"Steer owner"** (opens the
  team-message composer addressed to `owner_did`), not an edit form. The UI makes the
  "edit at rest / steer in flight" rule visible.
- **Drawer:** click a card → detail drawer (mirror `schedule-drawer.tsx`) to
  view/edit (at-rest) or steer (in-flight).
- **Filters + metrics + counts — FR-16:** the tasks page adds filter pills (owner /
  priority / status / tag — extend tasks.tsx's existing status `FilterPills`), a
  metrics row up top (in-progress · done-today · avg time-to-done · failed), and a
  counts strip below (tasks · inbox · blocked · backlog). All computed from the
  arcstore `tasks` query + existing inbox/audit reads — no new backend aggregation.
- **Activity timeline — FR-12:** the drawer renders the task's audit history
  (created/assigned/started/completed/failed + actor + ts) from `observe.audit`
  filtered by `target == task_id`.
- **Run + cost link — FR-11:** an in-progress/done card links `run_id` → the existing
  run/trace view; cost + tokens + owning agent come from that run's `llm_calls`
  (`observe.timeline` already joins on `request_id == run_id`) — no new endpoint.
- **Structured output — FR-13:** the drawer shows the task's `output` (summary +
  artifact links) on `done`.

## 7. Security + audit — NFR-3, NFR-4

- Agent tools: owner-only mutation; create/assign resolve a real teammate DID or fail;
  free-text sanitized (LLM01/ASI06); every op audited with `actor_did`.
- Cross-agent assignment Ed25519-signed via arcteam (ASI07).
- arcui mutations operator-gated (viewer read-only); in-progress edit blocked (NFR-4);
  every human write audited with operator DID.
- Atomic claim prevents double-ownership (G3/NFR-2).

## 8. CLI surface — FR-14 (arccli)

`arc task` subcommands mirror the tools + arcui operator ops, calling the SAME
arcstore task seam (not a parallel path): `create`, `list [--scope --status --owner]`,
`edit <id>` (at-rest only — refuse if `in_progress`), `assign <id> <@handle>`,
`complete <id>`, and `talk <id>` (steer the owner — routes an arcteam message to
`owner_did`, NOT a task edit; same edit-at-rest / steer-in-flight rule as arcui). Human
CLI writes are operator-gated (invoking DID must be an operator) + audited
(`arctrust.audit.emit`, `actor_did`). Reuses arccli's existing `team` command patterns
for `@handle` resolve + signing.

## 9. Decomposition + spawn — FR-15

`decompose_task(id, subtasks[])` creates each sub-task with `parent_id=id`; the parent
gains `blocked_by=[sub_ids]` so it can't `complete` until they're `done` (FR-9). Two
run paths: (a) the **same agent** works the sub-tasks as its dependency chain (FR-5
exemption — no cap violation); or (b) each sub-task is `assign`-ed to a **spawned child
agent** via arc's spawn/delegation seam (`ArcSpawnError` path), so a parent fans work
out to children and the board shows the tree. The coordinator that *decides* when to
decompose/spawn is ORCH-3 (deferred); the mechanism (sub-tasks + assign-to-child) is v1.

## 10. Threat mapping (tech.md compliance — every spec maps ≥1 threat ID)
- **ASI03 Identity/privilege abuse** → single `owner_did`, owner-only mutation, atomic claim (no ownership races).
- **ASI07 Insecure inter-agent comms** → `task.assigned` Ed25519-signed + replay-checked (arcteam).
- **LLM01/ASI06 Injection/context poisoning** → NFKC + injection-regex sanitize of all task free-text; typed patch-body validation.
- **AU (NIST audit)** → every create/transition/assign/claim/human-mutation emits an audit event (actor, action, target, outcome); the activity timeline (FR-12) is that trail surfaced.

## 11. Open design notes for /implement
- Exact `mutable_records` vs dedicated `tasks` table shape on the Phase-0A plane (conditional-write helper is shared either way).
- Whether `claim_task`, the `task.assigned` handler, and `decompose_task` child-adopt share one "adopt task" path.
- `max_active` config location (module config now; arctrust policy later).
