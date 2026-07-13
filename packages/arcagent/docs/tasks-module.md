# The Tasks Module — Mission Control (SPEC-056)

`arcagent.modules.tasks` gives an agent a task list — its own, plus the team backlog —
and, when opted in, autonomously *runs* that work with a full reliability engine. Tasks are
durable in the shared `arcstore` `tasks` directory (the mutable plane), so a task created by
an agent tool, the `arc task` CLI, or the arcui kanban is the **same row** all three read and
write. This document is the operator/builder reference: the lifecycle, the dispatch and
reliability engine, decomposition, routing, review, notifications, the API, and every config
knob.

For the model and store internals see `packages/arcstore/README.md`
(`arcstore.tasks` — the `Task` model + `TaskStore`). For how coordination signals ride the
team bus see `packages/arcteam/README.md`.

---

## Enabling it

```toml
[modules.tasks]
enabled = true

[modules.tasks.config]
# --- autonomy ---
dispatch = false                 # opt into autonomously running assigned tasks (see below)

# --- reliability (only relevant when dispatch = true) ---
default_max_attempts = 3         # retry ceiling stamped on tasks this agent creates
retry_backoff_seconds = 30.0     # base backoff before a retried task re-dispatches; grows exponentially
task_timeout_seconds = 0.0       # wall-clock cap on one run; 0 = unbounded
stuck_reclaim_seconds = 300.0    # an in_progress task with no live run is reclaimed after this

# --- routing + notifications ---
routing = true                   # auto-route ownerless tasks to the best eligible agent
notify = true                    # operator alerts on key transitions; assignee notify on assign

# --- wiring ---
nats_url = "nats://127.0.0.1:4222"  # live arcteam registry + messenger for @handle resolve + notify
data_dir = ""                    # defers to resolve_data_dir() so agent + arcui agree on the DB file
```

`[modules.tasks] enabled = true` loads the ten tools. **`dispatch` is a separate, explicit
opt-in** — merely loading the module never makes an agent run assigned work on its own;
autonomous execution is agency the operator grants deliberately (LLM06/ASI01).

---

## The lifecycle

```
                 assign / route             start_task (dispatch or tool)
   ┌─────────┐   ─────────────►  ┌──────┐   ─────────────────────────►  ┌─────────────┐
   │ backlog │                   │ todo │                               │ in_progress │
   └─────────┘  create(unowned)  └──────┘   ◄─────────────────────────  └─────────────┘
        │                           ▲          requeue (retry, backoff)     │      │
        │   create(owned)           │                                       │      │
        └───────────────────────────┘                                      │      │
                                                        complete_task ──────┘      │
                                                                                   │
                             requires_review? ──yes──►  ┌────────┐  approve ──►  ┌──────┐
                                        │               │ review │              │ done │
                                        no              └────────┘  reject ─┐   └──────┘
                                        │                    (→ todo) ◄─────┘
                                        └──────────────────────────────────►  done

   fail_task / timeout / error / stuck-reclaim / cancel  ──────────────────►  ┌────────┐
                                                       (retries exhausted)     │ failed │
                                                                               └────────┘
```

| Status | Meaning |
|---|---|
| `backlog` | Created unowned — the team pool, grabbable by anyone (`claim_task`) or auto-routed |
| `todo` | Owned and at rest, in the owner's ready lane — the dispatch loop picks from here |
| `in_progress` | A run is live (or was — see stuck-reclaim). One at a time per agent |
| `review` | Completed but held for a human decision (only when `requires_review`) |
| `done` | Terminal success |
| `failed` | Terminal failure (agent gave up, or retries exhausted / cancelled) |

**Transitions are deterministic and race-safe.** Every contended change (claim, assign, start,
requeue, dead-letter, route, review approve/reject) is a status-conditional atomic write
(`update_if`) in `TaskStore`, so two contending actors resolve to exactly one winner and the
loser no-ops — never a silent clobber.

### `create` defaults

`create_task` with an owner (self by default, or a teammate `@handle`) lands in `todo`; created
**unowned** (`owner=""`) it lands in `backlog`. An explicitly passed status is never
second-guessed.

### run_id linkage

When a task starts, the dispatch loop pins a fresh `run_id` up front and stamps it in the *same*
atomic write that claims the task, then hands that same id to the agent run. The run's spooled
events carry it too, so the arcui activity timeline joins `task.run_id` to the run — an
`in_progress` task deterministically links its run from the moment it starts. `complete`/`fail`
patch only status + resolution, so the link survives to `done`/`failed`.

### Timing + attempt fields

`started_at` / `completed_at` / `duration_seconds` are stamped on the transitions (the board's
DONE-TODAY / AVG-TIME metrics read these durable fields, not inferred timestamps). `attempts`
counts starts; `max_attempts` is the retry ceiling; `last_error` and `next_attempt_at` carry the
last failure and the backoff gate.

---

## The ten tools

| Tool | Class | What it does |
|---|---|---|
| `create_task` | state_modifying | Create a task owned by self (default), a teammate (`@handle`), or unowned (`owner=""`). `blocked_by` cycle-checked |
| `update_task` | state_modifying | Update title/description/priority on an owned, **at-rest** task |
| `start_task` | state_modifying | Start an owned (or unowned → self-claimed) task → `in_progress` |
| `complete_task` | state_modifying | Mark owned task `done` (or `review` if `requires_review`), with resolution + optional structured output |
| `fail_task` | state_modifying | Mark an owned task `failed` with a short resolution |
| `assign_task` | state_modifying | Reassign an at-rest task to a teammate (`@handle`); atomic single-owner; notifies + wakes the assignee. Rejects `in_progress` |
| `claim_task` | state_modifying | Pull the next available task for self (highest priority, deps met), respecting the in-progress cap |
| `list_tasks` | read_only | List tasks scoped to `self` or the whole `team`, optionally filtered by status |
| `decompose_task` | state_modifying | Break an owned task into subtasks; the parent becomes `blocked_by` them |
| `set_task_output` | state_modifying | Attach a structured result (`{summary, artifacts}`) to an owned task |

All mutations are **owner-only** (guarded against the runtime identity) except `create` (no prior
owner) and `assign` (reassignment is not the current owner's call). Free text is NFKC +
injection-sanitized at the arcstore boundary, so a prompt-injection payload in a title/description
raises a validation error the tool returns as a clean `{"error": …}` — every construction path
(tool, CLI, arcui) is sanitized identically.

---

## Autonomous dispatch

With `dispatch = true`, a background loop (`tasks_dispatch_loop`, `15s` cadence) drives the
agent's own assigned work:

1. **No-op** unless `dispatch` is on and a run callback is bound (delivered on `agent:ready`).
2. **Cap guard** — if a task is already `in_progress` for this agent, leave it be.
3. Take the agent's `todo` tasks, keep the **dispatchable** ones (retry backoff elapsed, all
   `blocked_by` dependencies `done`, and *not* a coordinator parent that has subtasks — a parent
   reconciles from its children, it never runs).
4. Pick the highest priority (critical → low), tie-broken by creation time.
5. Pin a `run_id`, atomically `start_task` (→ `in_progress`), and run it under the reliability
   wrapper. If the atomic claim was lost to a concurrent starter, try again next tick.

The run inside each tick is awaited, so dispatch is serial — the loop does not tick again until
the current task's run returns. That is what holds the one-`in_progress`-task cap without extra
locking.

---

## The reliability engine

A separate faster watcher (`tasks_reliability_watcher`, `5s` cadence) makes runs recoverable and
operator-stoppable. Both loops are gated by `dispatch`.

- **Retry with backoff** — a run that times out, errors, or is reclaimed is a failed attempt.
  Below `max_attempts` the task is requeued to `todo` gated by an exponential backoff
  (`retry_backoff_seconds * 2^(attempts-1)`), so a flapping task never hot-loops. At/above the
  ceiling it is **dead-lettered** to terminal `failed`.
- **Timeout** — `task_timeout_seconds` (or the per-task `timeout_seconds` override) caps a single
  run; exceeding it is a failed attempt fed to the retry path. `0`/unset = unbounded.
- **Stuck-task reclaim** — an `in_progress` task with no live run (the process crashed or was
  restarted mid-run) is reclaimed as a failed attempt: immediately on the first watcher pass after
  a restart (any pre-restart orphan), and thereafter once it is past `stuck_reclaim_seconds`. A
  live, healthy run is never touched.
- **Cancel / kill** — an operator sets `cancel_requested` (via the arcui/CLI cancel route). The
  watcher stops the live run through its tracked handle and dead-letters the task; if no run is
  live (e.g. after a restart), it finalizes the task directly. Genuine process shutdown is
  distinguished from an operator cancel so shutdown re-raises rather than being swallowed.

Every reclaim/dead-letter also fires an operator alert (below).

---

## Decomposition + dependencies

- **`decompose_task`** builds *all* subtasks before persisting any (a bad title in a later subtask
  can't orphan earlier ones), then makes the parent `blocked_by` the new subtask ids. The whole
  `blocked_by` set is cycle-checked before any write.
- **Parent roll-up** — each reliability tick reconciles a parent this agent owns from its
  children: it **auto-fails** the moment any child fails terminally (a failed subtask makes the
  parent unachievable — fail-fast), and **auto-completes** only when every child is `done` *and*
  the parent's full `blocked_by` set is satisfied (a parent may also carry non-child deps).
  Multi-level DAGs settle bottom-up over successive passes.
- **Dependency-gated dispatch** — a `todo` task is not dispatchable until every `blocked_by`
  dependency is `done`.
- **Cycle detection** — `create_task` and `decompose_task` reject a `blocked_by` set that would
  form a cycle before it is ever written (a cyclic task could never become ready).

The one-`in_progress` cap is exempt for a task's own dependency chain (a `blocked_by` relative or
a shared/parent `parent_id`) — it's the same piece of work, not a second independent task.

---

## Auto-routing

With `routing = true` and a live registry, each reliability tick routes every ownerless
(`backlog`/`todo`, no owner) task to the best eligible agent:

- **Candidates** are active `agent` entities from the arcteam registry.
- **Selection** prefers a capability match (task `tags` ∩ agent `capabilities`), then least loaded
  (count of that agent's `todo` + `in_progress` tasks), then name. A capability match dominates
  load, so a matching-but-busier agent still wins.
- Load is tracked incrementally across the pass, so a burst of tasks spreads rather than piling on
  the momentary least-loaded. Selection is deterministic, so concurrent routers on different
  agents converge; the store's owner-null-conditional `route` lands exactly one write, and the
  routed-to agent gets a `task_assigned` DM.

Goal-relevance routing would slot in here as a richer score — the seam is `_pick_agent`.

**Handoff / reassign** of an already-owned task is `assign_task` (agent) or the owner-`PATCH`
route (arcui) — distinct from routing, which only places *unowned* work.

---

## The review gate

Opt-in per task via `requires_review`. When set, `complete_task` lands the task in `review` (not
`done`) and notifies the operator "needs review". An operator then **approves** (→ `done`, stamps
the terminal timing the gate deferred) or **rejects** (→ `todo`, clears the backoff so the owner's
dispatch loop re-runs it promptly). This is the human-in-the-loop gate for consequential or
irreversible results (LLM06/ASI09).

---

## Notifications

Best-effort and classification-aware, gated by `notify` and the presence of a live messenger:

| Event | Recipient | Type |
|---|---|---|
| Task assigned / routed to an owner | the assignee (`@handle`) | `task_assigned` |
| Task `done` | `user://operator` | `info` |
| Task needs review | `user://operator` | `info` |
| Task `failed` | `user://operator` | `alert` |
| Task dead-lettered (retries exhausted) | `user://operator` | `alert` |
| Stuck task reclaimed (escalation) | `user://operator` | `alert` |

A notification carries the task's `classification` onto the envelope so the messenger's
no-write-down check engages (an `UNCLASSIFIED` default would leave it inert). Delivery is
best-effort: the durable store write is already truth and audited, so a failed notify is logged
and swallowed — it never blocks or rolls back the transition.

---

## Config reference

Loaded from `[modules.tasks.config]`. All fields have safe defaults; `extra="forbid"` catches typos.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Config-level enable mirror (the load gate is `[modules.tasks].enabled`) |
| `dispatch` | `false` | **Opt-in**: run the agent's ready owned tasks autonomously. Off = the tools work but nothing self-runs |
| `default_max_attempts` | `3` | Retry ceiling stamped onto tasks this agent creates (`1` disables retry). The per-task `max_attempts` is authoritative once set |
| `retry_backoff_seconds` | `30.0` | Base backoff before a retried task re-dispatches; grows exponentially per attempt (`base * 2^(attempts-1)`) |
| `task_timeout_seconds` | `0.0` | Wall-clock cap on one dispatched run; `0` = unbounded. Per-task `timeout_seconds` overrides. A timeout is a failed attempt |
| `stuck_reclaim_seconds` | `300.0` | An `in_progress` task with no live run older than this is reclaimed as a failed attempt (startup reclaim ignores the threshold) |
| `routing` | `true` | Auto-route ownerless tasks to the least-loaded, capability-matched agent. No-op without a live registry |
| `notify` | `true` | Operator alerts on done/needs-review/fail/dead-letter/stuck, and assignee notify on assign/route |
| `nats_url` | `""` | JetStream url for the shared arcteam registry + messenger. Empty = no live registry (`@handle` resolution + notify degrade with a clear error) |
| `data_dir` | `""` | Forwarded to `arcstore.config.resolve_data_dir`; empty defers to env > default so the module and arcui agree on the SQLite file |

---

## The arcui API

arcui reads and steers the same shared `tasks` directory over a small REST surface
(`packages/arcui/src/arcui/routes/tasks.py` + `team_pages.py`). Mutations are **operator-only**
(a viewer token gets `403`) and audited whichever way they resolve.

| Method + path | Purpose |
|---|---|
| `GET /api/team/tasks` | The team kanban — every task row, each stamped with its owning `agent_id` |
| `GET /api/agents/{id}/tasks` | One agent's task rows |
| `POST /api/team/tasks` | Create a task. Body fields: `title`, `description`, `priority`, `owner_did`, `tags`, `requires_review` |
| `PATCH /api/tasks/{id}` | Edit an **at-rest** task (allowlisted: `title`, `description`, `priority`, `owner_did`, `tags`, `requires_review`). Reassign an owner by patching `owner_did`. `409` if the task is `in_progress` |
| `DELETE /api/tasks/{id}` | Remove a task in any state (destructive, operator-gated) |
| `POST /api/tasks/{id}/cancel` | Request an operator stop of a running task (sets `cancel_requested`; the agent's reliability watcher stops the run) |
| `POST /api/tasks/{id}/approve` | Approve a review-gated task: `review` → `done` |
| `POST /api/tasks/{id}/reject` | Reject a review-gated task: `review` → `todo` for re-dispatch |

The `PATCH` allowlist deliberately excludes `status`, `id`, `created_at`, `run_id`, and
`blocked_by` — those are managed only by the store's own transitions, never by a client-supplied
key (SEC-F4). Editing an `in_progress` task is refused (`409`) with the same edit-at-rest rule the
tools and CLI enforce: steer a running task through its owner, don't edit it underneath the run.

---

## Operating from the CLI

The `arc task` group is the operator surface over the same shared store (see
`packages/arccli/README.md`):

```bash
arc task create "Draft the Q3 report" --actor @lead --priority high   # unowned -> team backlog
arc task create "Ship it" --actor @lead --owner @analyst-1            # assigned at creation
arc task list --scope mine --actor @analyst-1                        # omit --scope for the team view
arc task edit <task_id> --actor @lead --priority urgent               # at-rest only — 409 if in_progress
arc task assign <task_id> @analyst-1 --actor @lead                    # atomic; notifies + wakes assignee
arc task complete <task_id> --actor @analyst-1 --resolution "done"
arc task talk <task_id> "any update?" --actor @lead                   # steer an in-progress owner (not an edit)
```

Human writes are operator-gated (the `--actor` entity must carry the `operator` role) and audited
through a WORM sink. `edit` refuses an `in_progress` task (steer it with `arc task talk`, not edit)
— the same edit-at-rest / steer-in-flight rule the tools and arcui enforce.
