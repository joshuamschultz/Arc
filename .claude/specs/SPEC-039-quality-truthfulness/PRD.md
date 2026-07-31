# SPEC-039 PRD — Quality / Truthfulness Pass (code)

## Problem
The monorepo has four quality regressions blocking a clean state:
- arcagent/core is 3569 NCLOC — 69 over the 3500 budget gate (G1.5).
- arcskill fails `mypy --strict` (8 env-driven errors; no `[tool.mypy]` config).
- A 3+-package `--import-mode=importlib` pytest run fails at collection (duplicate
  `tests.conftest` module name across arctrust + arccli).
- SPEC-038 shipped the budget mechanism but no default ceilings, so budgets are
  operator-only (OFF by default) — a federal agent with no budget block is unbounded.

## Requirements (EARS)
- REQ-001: The system SHALL keep `arcagent/core/*.py` total NCLOC < 3500 (target ≤ ~3480)
  by relocating non-nucleus code to its correct home, with no behavior change.
- REQ-002: The arcskill package SHALL pass `mypy --strict src/arcskill` in the
  extras-absent environment via a minimal `[tool.mypy]` config, without installing extras
  and without breaking the extras-present environment.
- REQ-003: WHEN pytest runs 3+ packages with `--import-mode=importlib`, the system SHALL
  collect and pass, WITHOUT breaking any package's own `pytest -q`.
- REQ-004: WHERE a tier is federal or enterprise and the operator sets no budget ceiling,
  the system SHALL apply a conservative default token/cost/request ceiling; personal SHALL
  remain unbounded when unset; an explicit operator ceiling SHALL always win.

## Non-goals
- The DOCS/truthfulness sweep (READMEs, docstrings) — separate later worker.
- Any change to arcrun/arcteam/arcgateway behavior.

## Priorities (MoSCoW)
- Must: REQ-001, REQ-002, REQ-003, REQ-004.
</content>
