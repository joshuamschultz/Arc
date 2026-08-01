# Specification: SPEC-061 ArcFlow — Named, Signed, Conversationally-Authored Workflows

**Feature:** `arcflow` (spec folder `SPEC-061-arcflow`)
**Created:** 2026-08-01

**Documents:** [DESIGN.md](DESIGN.md) (deepened design, 15 research-insight blocks) · [PRD.md](PRD.md) (REQ-217..REQ-251) · [SDD.md](SDD.md) (COMP-001..COMP-020) · [PLAN.md](PLAN.md) (T-826..T-867, 42 tasks, 4 phases)

## Status

| Doc | Status | Last Update |
|---|---|---|
| PRD | draft | 2026-08-01 |
| SDD | draft | 2026-08-01 |
| PLAN | draft | 2026-08-01 |

## Steering References

- Product: [`../../steering/product.md`](../../steering/product.md)
- Tech: [`../../steering/tech.md`](../../steering/tech.md)
- Structure: [`../../steering/structure.md`](../../steering/structure.md)
- Roadmap: [`../../steering/roadmap.md`](../../steering/roadmap.md)

## Decision Log Snippets

37 decisions logged in [`../../decisions-log.md`](../../decisions-log.md) as **D-501 to D-537** (5 auto-applied under the fedramp/nist regime). The load-bearing ones:

- **D-506** — the workflow engine lives in `arcteam`; workflows are narrowed multi-agent coordination.
- **D-507** — a run instantiates onto the existing task DAG; ArcFlow is not a third DAG engine.
- **D-508** — a dedicated deterministic fleet runner; no orchestrator agent, models never sequence.
- **D-514** — lazy frontier materialization; untaken branches never become tasks, the Run records the path taken.
- **D-523** — agents and UIs author unsigned drafts; only the operator signs, out-of-band.
- **D-524** — capability legs accumulate per run, not per node session (the gate-bypass fix).
- **D-536** — a workflow is a group chat of its agents and people; gates answerable in channel.
- **D-538** — handoff is the task-row write that names the next owner, never a message; messaging is wake-signal and narration only. A run must complete with every outbound message dropped.

## Phase Notes

_(append `### Phase N: <name>` blocks via `append_phase_note.py` at phase boundaries during `/implement`.)_

## Learnings

Feature-specific insights captured here. Global / reusable patterns go to memory via `/memorize`.

_(none yet)_

## Open Questions

1. Channel binding granularity: one channel per workflow with a thread per run, or a channel per run?
2. Dashboard editor scope for the graph phase: full graph editing, or node-property editing over a rendered graph first?
3. Which event trigger lands first after v1 — message, webhook, or task-created?
4. Dependency posture: declare `arcstore` and `arcteam` in arcagent's project metadata, or keep lazy imports with graceful degradation? (T-853 resolves.)
