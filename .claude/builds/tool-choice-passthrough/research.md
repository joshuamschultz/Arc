# tool-choice-passthrough — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-384–D-386 (3 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## tool_choice passthrough — review follow-ups (2026-06-18)

Source: `/review` of `feat/restore-tool-choice-passthrough` (merged to main `2dbaba8`, develop `b3250aa`). The passthrough itself landed clean (PASS); the items below are **inherited** debt the review surfaced in arcrun/arcllm. Deliberately **not** fixed in the arcagent PR — the fixes cross package boundaries (arcllm owns the concept), and folding them in would violate "don't mix modules/packages." Tracked here for a dedicated arcllm-owned change.

| # | Decision | Choice | Priority | Rationale | Tier Notes |
|---|----------|--------|----------|-----------|------------|

#### Ownership

All three are **arcllm/arcrun-owned** (D-384 originates in arcllm; D-385/D-386 in arcrun). The arcagent surface (`run`/`run_collected`/`dispatch_stream`) only forwards the value and is correct as-is. See memory `project_arcllm_owns_wire_types`.

---

---
