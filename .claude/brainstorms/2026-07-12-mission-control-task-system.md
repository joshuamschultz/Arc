# Brainstorm — Mission Control: a shared multi-agent task system for arc

**Date:** 2026-07-12 · **Status:** BRAINSTORM (→ /deepen → /specify) · **Roadmap:** ORCH-2 ([[project_orchestration_roadmap]])
**Inputs:** Anthropic Agent-Teams task list + mission-control repo research ([[reference_claude_multiagent_patterns]]), arc's existing foundation.

---

## 1. WHY — the coordination gap

arc agents can **message** each other (signed, durable, over NATS) but cannot **coordinate work**. There is no shared surface where a unit of work is created, owned, tracked, handed off, and verified. Coordination today happens in lossy chat; a coordinator would have to *read messages* to know what's done. Anthropic's #1 finding: **the task list, not chat, is the coordination channel.** This is the keystone that turns "agents that talk" into "agents that get work done together."

The mental model to hit (Josh's words): *an agent lists its tasks and knocks them out — doing some itself, spawning/handing off others — and a human watches the whole team's board move.*

## 2. Vision

- Every agent has **its own task list**, in **its own harness** (arcagent module) — self-created tasks it works through, plus tasks **assigned to it by teammates**.
- An agent can **create a task for itself → do it → mark done**, or **assign a task to another agent** when the work belongs to them.
- arcui shows **each agent's tasks in its detail view**, and a **team-wide kanban** (all agents' tasks, by status) in a Tasks view — the human's mission-control window.
- Tasks are the **engine of autonomous work**: a task can drive an agent run (like a self-scheduled prompt), so "working the board" *is* the agent loop.

## 3. What already exists in arc (we are NOT starting from zero)

- **arcui READ half is built.** `/api/team/tasks` aggregates every agent's `tasks.json` stamped with agent_id (`routes/team_pages.py`); `/api/agents/{id}/tasks` serves one agent's (`routes/agent_detail/sessions.py`); `TasksResponse` schema exists. So the team-kanban + per-agent read endpoints are already there — they just read an empty/absent `tasks.json` today.
- **The scheduler module is the exact architectural template.** `arcagent/modules/scheduler/` = models / store (`schedules.json`) / capabilities (create/update/delete tools) / _runtime / scheduler(driver). A `tasks` module mirrors it 1:1: models / store (`tasks.json`) / capabilities (create/update/complete/assign tools) / _runtime.
- **arcstore is the durable directory** (SPEC-032 decision: entities/teams/channels live in arcstore, source of truth arcui reads). Tasks are directory data → they belong in the same place.
- **arcteam** carries signed cross-agent messages — the transport for "assign this task to teammate B."
- **arcui push** (existing `/ws`) already streams live updates from arcstore — the kanban rides it, no new realtime infra.

**Net missing:** the **write half** (arcagent `tasks` module + tools), **cross-agent assignment** (arcteam envelope), the **durable team store** (arcstore table vs per-agent json — decision below), and the **kanban UI**.

## 4. Design space + recommendations

### 4a. Task data model
Synthesize mission-control's fields + Agent-Teams' dependency edges + a liveness field (mission-control's documented weak spot):

```
Task:
  id: str                      # stable, e.g. "{agent}-{n}" or uuid; human ref optional
  title: str
  description: str
  status: <enum, see 4b>
  priority: low|medium|high|critical
  owner_did: str               # the agent responsible (assignee)
  creator_did: str             # who created it (self or another agent / operator)
  blocked_by: list[str]        # dependency edges (Agent-Teams) — mission-control LACKS this, we add it
  run_id: str | None           # liveness: the live run working it (their conflation of identity+liveness = don't repeat)
  tags: list[str]
  metadata: dict               # opaque
  created / updated: iso ts
  resolution: str | None       # why done/failed
```
- **Adopt from mission-control:** `failed` is a *distinct terminal state*, not a flag on done; index only the 3 real queries (status, owner, recency); `assigned_to` as ONE field with three populate-paths (§4c).
- **Add (their gap):** `blocked_by` dependency edges + atomic claim (Agent-Teams). Separate stable `owner_did` (identity) from `run_id` (liveness) — don't conflate as they did.

### 4b. Status model — richer enum than board columns
Their lesson: keep more states than columns; collapse at render. Proposed arc set (leaner than their 9):
`backlog → todo → in_progress → review → done` + terminal `failed` (+ optional `blocked` derived from `blocked_by`).
- Board columns render a subset; `review` is the **generic completion-gate hook point** (NOT a hardcoded "Aegis" stage) — this is exactly where the deferred **ORCH-3 completion-verification** plugs in later. Model the gate as a hook/policy point now, fill it later.

### 4c. Assignment — one field, three paths
1. **Create-assigned** (push): create a task with `owner_did = teammate` → lands in their list as `todo`.
2. **Unassigned/inbox**: create with no owner → sits in team backlog for triage/claim.
3. **Self-claim** (pull): an agent asks for the next unblocked task by priority. **Adopt mission-control's self-describing `reason` response** — `assigned | continue_current | at_capacity | no_tasks_available` — so polling is idempotent and the agent always knows *why* it did/didn't get work. **Adopt atomic claim + per-agent concurrency cap** (surface `at_capacity` explicitly).

### 4d. Storage — the one real architecture decision
Two coherent options:
- **(A) Per-agent `tasks.json` (mirror scheduler), arcstore aggregates for the team read.** Pro: matches the existing arcui read-half + scheduler template exactly; agent owns its file; least new code. Con: cross-agent assignment = writing another agent's file (or messaging them to write it); team kanban = scatter-gather over N files (already how `/api/team/tasks` works).
- **(B) arcstore is the single durable task directory (per SPEC-032 philosophy); agents read/write via a store seam; NATS pushes updates.** Pro: one source of truth, real queries/indexes, clean cross-agent assignment (write a row), live kanban is a table read. Con: more new code; per-agent `tasks.json` read-half becomes a projection, not the source.
- **Recommendation to explore in /deepen:** **(B) with a thin per-agent projection.** SPEC-032 already made arcstore the durable directory for entities/teams/channels; tasks are the same shape of data and cross-agent assignment is far cleaner as a row write than a foreign-file write. Keep `tasks.json`/`/api/agents/{id}/tasks` as a **read projection** so the existing arcui read-half keeps working. (If we want to ship fast, (A) is the lower-risk MVP — decide in deepen.)

### 4e. Cross-agent assignment mechanism
Assigning to a teammate must respect concern separation:
- The **fact** of the task lives in the durable store (arcstore, option B).
- The **notification** ("you've been assigned X") rides **arcteam** as a signed envelope → lands in the assignee's inbox → (with SPEC-055 triage) wakes them because they're the addressed owner. This reuses everything we just built.

### 4f. Task-as-spawn-point / the work loop
mission-control lets a kanban card spawn a sub-agent. arc's analog (arc has **persistent teammates**, not ephemeral Task-tool subagents): an agent working a task can (a) do it directly, (b) **decompose it into sub-tasks** (`blocked_by` children) it keeps, or (c) **assign sub-tasks to teammates**. A task can **drive a run** (like the scheduler drives a run from a schedule) — this is how "working the board" becomes the autonomous loop. The coordinator that orchestrates this decomposition is **ORCH-3 (deferred)** — the task system is the substrate it will later drive.

### 4g. Realtime + UI
- **Kanban** (team Tasks view) + **per-agent task list** in agent detail. Columns from §4b; cards show title/priority/owner/blocked/run-link.
- Push via the **existing arcui `/ws`** off arcstore (SPEC-026 pattern — arcui pulls from arcstore DB; no parallel push wire). "Zero stale data" like mission-control, but on infra we already have.
- Human can create/reassign/reprioritize from the board (operator-gated, audited) — parallels the SCHED2 "edit from dashboard" seam (which doesn't exist yet; tasks should ship with the mutation seam SCHED lacked).

## 5. Resolved decisions (/deepen, 2026-07-12 — Josh)
1. **Storage → (B) arcstore.** arcstore is the single durable task directory (SPEC-032 philosophy); per-agent `tasks.json` / `/api/agents/{id}/tasks` becomes a **read projection** so the existing arcui read-half keeps working. No per-agent json source-of-truth.
2. **Assignment → single exclusive owner, no competition.** A task is either **assigned** to an agent (push) or **grabbed** by one (self-claim), but has **exactly one owner** — two agents never contend for the same task. Enforced by an **atomic claim** in arcstore (claim = conditional write: set `owner_did` only if currently unowned; loser gets `no_tasks_available`/`continue_current`, never a double-grab). No bidding/competition model.
3. **Concurrency → one `in_progress` task per agent; dependency chains exempt (Josh, 2026-07-12).** Agents run concurrently (shared-nothing); each agent works exactly ONE `in_progress` task at a time. A `claim`/start of a NEW *independent* task while active returns `continue_current`. **Exception:** a task's own dependency chain — its `blocked_by` targets, or sub-tasks it decomposed — is NOT a competing second task; the agent may own the chain and work it one-at-a-time in dependency order. The cap is on `in_progress`, **not on ownership**. *Cap = 1 in_progress (v1); bump via config later if desired.*
4. **Human mutation → at-rest editing + steer-in-flight.** A human CAN adjust a task that is **not in progress** (edit title/priority/owner/status while it sits in backlog/todo/blocked) via an operator-gated, audited arcui route. A human does **NOT** directly edit an **in-progress** task — instead they **message the owning agent to steer it** (reuses arcrun steer/follow_up + SPEC-055). Clean rule: *edit at rest, steer in flight.*
5. **Dependency edges → v1-functional for ordering + capacity (revised per #3).** `blocked_by` ships in the model AND drives v1 behavior: a task cannot be `started` until its `blocked_by` deps are `done` (ordering), and a dependency of the agent's current work is exempt from the one-active cap (#3). The heavier dependency-graph *visualization / blocking board UI* is still v2, but the claim/start/cap logic is dependency-aware in v1.
6. **Task → run trigger → pull/tool-driven v1.** An agent works its tasks through its own loop + tools; a task does not auto-spawn a run in v1. **Auto-drive arrives with the coordinator (ORCH-3, deferred).** *(Recommendation.)*
7. **Identity vs liveness → split confirmed.** `owner_did` (stable assignee) + `run_id` (the live run working it, nullable) — never conflated (mission-control's documented weakness).
8. **Completion gate → inert `review` hook now.** Ship `review` as a no-op gate point; ORCH-3's verification snaps in later with no rework.

## 6. Rough shape (the strawman to deepen)
- **arcagent `tasks` module** mirroring scheduler: `models.py` (Task), `store.py` (writes durable store + `tasks.json` projection), `capabilities.py` (tools: `create_task`, `update_task`, `complete_task`, `fail_task`, `assign_task`, `claim_task`), `_runtime.py`.
- **arcstore**: a `tasks` table in the durable directory (option B) + read API the arcui aggregation already expects.
- **arcteam**: a `task.assigned` signed envelope for cross-agent assignment → assignee inbox (SPEC-055 wakes the owner).
- **arcui**: keep the read endpoints; add the **kanban Tasks view** + per-agent list + a task drawer (mirror `schedule-drawer.tsx`) + operator-gated mutation routes.
- **Concern map:** durable truth = arcstore · per-agent tools/loop = arcagent · cross-agent transport = arcteam · view = arcui. No mixing.

**One-line pitch:** *mirror the scheduler module for a `tasks` module, back it with the arcstore directory, notify assignees over arcteam (SPEC-055 wakes them), and render a live kanban off the arcui read-half that already exists — the task list becomes arc's coordination channel and, later, what ORCH-3's coordinator drives.*
