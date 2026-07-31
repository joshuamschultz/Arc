# SPEC-039 — Quality / Truthfulness Pass (CODE portion)

Status: COMPLETE (code items). Branch: `feat/SPEC-039-quality-truthfulness` (off develop @ 0f1d77c).

Scope: the four CODE items only. The DOCS/truthfulness sweep (READMEs/docstrings) is a
separate later worker and is intentionally NOT done here.

## Items

1. **core-slim** (primary) — arcagent/core back under the 3500 NCLOC budget.
2. **arcskill mypy gate** — arcskill passes `mypy --strict` like the other packages.
3. **conftest collision** — 3+-package `--import-mode=importlib` run collects cleanly.
4. **per-tier default budget ceilings** (SPEC-038 OQ-3) — budgets ON by default per tier.

## Key learnings

- `scripts/check_loc_budgets.py` globs `core/*.py` **non-recursively**. Moving code
  between two files that both live in `core/` is net-zero for the budget. Only
  relocating OUT of `core/` (to `tools/`, `modules/`, `utils/`) reduces it. The
  reviewer's "agent.py dispatch helpers → agent_dispatch.py" candidate is therefore
  useless for the budget (both are core files).
- `core → tools` is an already-established import direction (tool_registry imports
  `tools/_policy_fill` and `tools/human_gate`), so moving tool-domain primitives from
  `core/` into `tools/` introduces no new/backwards layering edge.
- Distributing `config.py` per-feature Pydantic blocks to `modules/` would invert the
  layering (core importing modules); rejected in favor of the tool-primitives move.
</content>
