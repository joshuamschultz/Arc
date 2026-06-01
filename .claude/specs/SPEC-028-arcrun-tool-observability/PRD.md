# SPEC-028 — ArcRun Tool / Code / Spawn Observability: Product Requirements

## Problem Statement

After SPEC-026, arcui reads operational state from a durable store instead of a
fragile live push. But that store only carries **LLM calls** and **run
lifecycle**. The actual work arcrun performs — tool invocations, sandboxed code
execution, and spawned child agents — is emitted to a transient in-memory
`EventBus` and never persisted. Consequently:

1. **Code execution is invisible.** When an agent runs Python via arcrun's
   sandbox, the dashboard shows a turn happened and an LLM was called, but not
   *what code ran* or *what it produced*.
2. **Spawned agents are invisible.** `spawn_task` starts a child arcrun loop; the
   dashboard shows neither the child's existence, its input/output, nor the
   parent→child lineage.
3. **Parent and child LLM calls are conflated.** Because a spawned child reuses
   the parent's model + telemetry, the arcllm view attributes the child's calls
   to the parent. There is no way to see "the parent made 3 calls, its 2 children
   made 5 between them."

The result: arcui under-represents what arcrun can do. An operator watching a
multi-step, code-running, agent-spawning task sees a thin slice of reality.

## Vision

> **arcui shows everything arcrun does.** Every tool call, every line of code the
> agent executed (metadata-by-default, body opt-in), and every spawned child —
> with its own identity, its own LLM calls, and a visible parent→child lineage —
> is durably recorded the instant it happens and rendered in the dashboard.
> Observability depth matches execution depth.

## Audience

| Persona | Pain today | What this fixes |
|---|---|---|
| **Operator watching agents** | Sees LLM calls + turns, but not the tools/code/sub-agents doing the work | A per-run timeline of tool + code events and a spawn lineage tree |
| **Developer debugging a run** | Can't tell which code execution failed or what a spawned child returned | Durable tool/code records with outcome, latency, and (opt-in) body |
| **Cost/usage analyst** | Child-agent LLM cost is hidden inside the parent's totals | Per-identity attribution — parent vs each child separated |
| **Compliance / federal evaluator** | Code the agent executed isn't in the operational record; only the audit chain has spawn edges | A durable, metadata-default operational trail of executed code + delegation, consistent with the WORM |

## Use Cases

### UC-1 — See the code an agent ran
An agent solves a task by writing and executing Python through arcrun's sandbox.
The operator opens the run in arcui and sees each code execution: the snippet (if
`store_raw_bodies` is on) or its hash + summary (default), stdout/result
metadata, duration, and ok/error outcome — in order, interleaved with the LLM
turns that produced them.

### UC-2 — See a spawned agent's work
A parent agent calls `spawn_task` three times to decompose a job. arcui shows a
lineage tree: parent → 3 children. Clicking a child shows its own task prompt,
its own LLM calls, its own tool/code events, and its returned result.

### UC-3 — Separate parent vs child LLM spend
The cost view groups `llm_call` records by `agent_label`/`actor_did`. The parent
and each child appear as distinct rows with their own token/cost totals; the
parent's total no longer absorbs its children's calls.

### UC-4 — Tool timeline for any run
For any run (spawned or top-level), the operator sees an ordered timeline of
`tool.start`→`tool.end`/`tool.error` with tool name, argument metadata, result
metadata, latency, and outcome — the same way LLM calls already render.

### UC-5 — Air-gapped, metadata-only by default
On a SCIF box with `store_raw_bodies=false` (the SPEC-026 default), the tool/code
records carry **metadata only** — names, hashes, sizes, outcomes, latencies — no
code bodies, no tool argument values, no child prompts/outputs. Enabling raw
capture is an explicit, audited opt-in (reusing the existing flag + startup
warning).

## Functional Requirements

### FR-1 — Durable tool-event recording (arcrun → spool) (P0)

arcrun records its tool lifecycle to the operational spool, not just the
in-memory EventBus.

- A new spool kind `tool_event` captures `tool.start`/`tool.end`/`tool.error`.
- Fields (flat, per the SPEC-026 NFR-1 one-line-model rule): `tool_name`,
  `phase` (`start`/`end`/`error`), `outcome` (`ok`/`error`), `latency_ms`,
  `args_digest` (sha256 of canonical args), `result_digest`, `args_size`,
  `result_size`, and — **only when `store_raw_bodies=true`** — `args` and
  `result` bodies in `extra`.
- Recording is gated by the same `arcstore.enabled` switch and is **fail-open**
  (a spool write error never breaks the tool call), exactly like `llm_call`.
- arcrun imports only `arcstore.spool` (the SPEC-026 producer boundary holds).

**Acceptance criteria:**
- AC-1.1 — A run that calls a tool produces a `tool_event` spool record for start
  and for end (or error), with name/outcome/latency populated. **Pillar: Simplicity.**
- AC-1.2 — With `store_raw_bodies=false` (default), no tool argument or result
  *body* is written; only digests/sizes. **Pillar: Security.**
- AC-1.3 — A tool that raises records a `tool_event` with `phase="error"` and
  `outcome="error"`. **Pillar: Security.**
- AC-1.4 — arcrun still imports no backend (only `arcstore.spool`); import-graph
  test holds. **Pillar: Modularity, Scalability.**

### FR-2 — First-class code-execution detail (P0)

Sandboxed code execution (`make_execute_tool`) is distinguishable from other
tools in the record so the UI can give it a dedicated view.

- The `tool_event` for the execute tool carries the executed **code** as the
  args body (metadata-only by default: `code_digest` + `code_size`; full `code`
  only when `store_raw_bodies=true`) and the run **result/stdout** as the result
  body under the same gate.
- No new mechanism — this is the FR-1 record with the execute tool's
  conventional name recognized by the UI.

**Acceptance criteria:**
- AC-2.1 — A sandboxed code execution produces a `tool_event` identifiable as
  code-exec (by tool name), with `code_digest`/`code_size` always present. **Pillar: Simplicity.**
- AC-2.2 — The code body is present **only** when `store_raw_bodies=true`. **Pillar: Security.**

### FR-3 — Spawn identity + lineage (arcagent) (P0)

Spawned children get their own operational identity and the parent→child edge is
recorded operationally (not only in the audit chain).

- `spawn()` / `spawn_task` pass a distinct `actor_did` (the already-derived
  `child_did`) and a derived `agent_label` into the child `arcrun.run(...)` and
  into the child's telemetry, so the child's `run_event`s and `llm_call`s spool
  under the **child** identity.
- A `spawn_event` is recorded (new kind, or an `agent_event` subtype) capturing
  `parent_did`, `child_did`, `role`, `depth`, and outcome — the operational
  lineage edge the UI renders.
- arcrun is **not** modified for spawn (it stays a pure loop); only arcagent
  orchestration changes. The existing `_emit_spawn_audit` (arctrust) is retained.

**Acceptance criteria:**
- AC-3.1 — A spawned child's `llm_call` records carry the child's
  `actor_did`/`agent_label`, distinct from the parent's. *(Closes finding F4.)* **Pillar: Modularity.**
- AC-3.2 — A spawned child's `run_event`s are spooled under the child identity.
  *(Closes finding F5.)* **Pillar: Modularity.**
- AC-3.3 — A `spawn_event` records the parent→child edge (parent_did, child_did,
  role, depth) operationally. **Pillar: Security.**
- AC-3.4 — arcrun source is unchanged by this FR (spawn stays arcagent);
  import-graph/ownership test holds. **Pillar: Modularity.**

### FR-4 — arcui surfaces (P0)

arcui reads the new records and renders them. Read-only, pull-based — no new
transport (SPEC-026 D-007).

- Observe gains queries: tool/code events per run, spawn lineage for a run/agent,
  and per-identity LLM aggregation.
- The dashboard gains: (a) a per-run **tool/code timeline**, (b) a **spawn
  lineage tree** (parent→children, each node linking to its own run view), and
  (c) **parent-vs-child** separation in the LLM/cost views (group by
  `agent_label`).

**Acceptance criteria:**
- AC-4.1 — A run's tool/code events are retrievable via the Observe query layer
  and rendered in order. **Pillar: Simplicity.**
- AC-4.2 — The spawn lineage for a parent run is retrievable and rendered as a
  tree. **Pillar: Simplicity.**
- AC-4.3 — The LLM/cost view separates parent and child by identity. **Pillar: Modularity.**
- AC-4.4 — Killing/restarting the arcui server loses no tool/spawn history (it
  re-reads the durable store). **Pillar: Scalability.**

## Non-Functional Requirements

- **NFR-1 (Simplicity)** — `tool_event`/`spawn_event` are flat one-line spool
  records like `llm_call`; no nested envelopes.
- **NFR-2 (Security)** — Metadata-only is the default for every new body field
  (code, tool args, tool results, child prompts/outputs). Raw capture rides the
  **existing** `store_raw_bodies` flag + startup warning — no new opt-in path.
- **NFR-3 (Security/Scalability)** — All new recording is fail-open (AU-5) and
  off the hot path; a spool error never breaks a tool call, code run, or spawn.
- **NFR-4 (Modularity)** — Producer import direction unchanged
  (`arctrust ← arcstore ← {arcllm, arcrun, arcagent} ← arcui`). arcrun records
  tool events (its own EventBus); arcagent records spawn lineage; neither learns
  about the other's concern.
- **NFR-5 (Scalability)** — High-frequency tool loops can be sample-rated via the
  existing `[arcstore].sample_rate`; lifecycle + spawn edges are never sampled
  out (they're low-volume and structurally important).

## Out of Scope (this spec)

- Token-by-token streaming of tool output to the browser (separate stream off
  arcrun; this is the durable observability path).
- Replaying or re-executing recorded code from the UI.
- The run/chat method unification (that is SPEC-027, independent).
- A new push transport — pull-only, per SPEC-026 D-007.

## Traceability

| Requirement | Builds on | NIST control |
|---|---|---|
| FR-1 | SPEC-026 FR-2/FR-4 (spool + producer hooks) | AU-2, AU-3 |
| FR-2 | SPEC-026 FR-4 AC-4.5 (metadata-only default) | AU-3, LLM05 |
| FR-3 | SPEC-026 FR-4 (agent_did/agent_label) | AU-2, ASI03 |
| FR-4 | SPEC-026 FR-5 (Observe read plane) | AU-6 |
