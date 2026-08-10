# ongoing-daily-notes — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-408–D-418 (11 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)

**Phase**: build | **Status**: pending (SPEC-030) | **Priority**: simplicity > modularity > security > scalability
**Source**: SPEC-029 review found `agent:pre_compaction` orphaned; 2-agent web research on memory cadence.
Spec: `.claude/specs/SPEC-030-ongoing-daily-notes/`

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|

All tiers config-toggleable under `[memory]`; consolidation output sanitized (ASI-06); module-only (no arcrun/arcllm).

#### SPEC-030 /review follow-ups (2026-07-02) — applied

4-reviewer swarm (security/clean-code/architecture/QA). Security PASS, architecture PASS on the
invariant, clean-code FAIL (DRY + clocks). No critical/high; a consensus correctness+hardening
cluster fixed before merge.

| # | Decision | Choice |
|---|----------|--------|

Doc corrections (code was more correct than spec): config lives in `modules/memory/config.py`;
dropped `hook_active` guard (no-op for direct writes). Final: ruff 0, mypy --strict 0, arcagent 3312 passed.

---
