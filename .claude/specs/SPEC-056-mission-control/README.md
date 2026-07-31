# SPEC-056 — Mission Control (multi-agent task system)

**Status:** DRAFT (spec'd, ready to implement) · **Roadmap:** ORCH-2 · **Created:** 2026-07-12

Turns arc from "agents that message" into "agents that coordinate work": per-agent
task lists in each agent's harness + a team-wide kanban in arcui; tasks created for
self or assigned to teammates; single exclusive owner; humans edit at rest and steer
in flight.

## Documents
- **PRD.md** — goals, users, functional/non-functional requirements, success criteria.
- **SDD.md** — architecture (arcstore truth · arcagent tools · arcteam assignment · arcui view), data model, atomic claim, status machine, security/audit.
- **PLAN.md** — TDD phases A–E (arcstore → arcagent → arcteam+arcui → e2e/live).

## Origin
Brainstorm: `.claude/brainstorms/2026-07-12-mission-control-task-system.md` (decisions §5).
Research inputs: builderz-labs/mission-control + Anthropic Agent-Teams task list
([[reference_claude_multiagent_patterns]]).

## Key decisions (Josh)
Storage = **arcstore** (single truth) · assignment = **single exclusive owner, atomic
claim, no competition** · concurrency = **1 active task/agent (v1)** · human = **edit
at rest, steer in flight** · deps = **schema now, enforcement v2** · task→run =
**pull/tool-driven v1** (auto-drive with ORCH-3) · `review` = **inert gate** for ORCH-3.

## Dependencies
- **ABSORBED as Phase 0 (built first):** the SPEC-032 **mutable arcstore plane** (Phase 0A)
  and **SPEC-055 mention-scoped activation** (Phase 0B) — the two dependency risks the
  deepen surfaced are now in-scope, so no external spec must land first.
- **ORCH-3** (coordinator + completion verification) — deferred; hooks (`review`,
  task→run) ship inert so it drops in without rework.
