# SPEC-039 PLAN — Quality / Truthfulness Pass (code)

Status: COMPLETE

## Tasks
- [x] T-1 (REQ-001) Move `core/tool_transport.py` → `tools/_transport.py`; update the sole
      importer (`core/tool_registry.py`) import + docstring; keep re-exports. Verify
      `check_loc_budgets.py` PASS (3411 / 3500).
- [x] T-2 (REQ-002) Add `[tool.mypy]` + sigstore override to `arcskill/pyproject.toml`; add
      `__all__` to `hub/_docker.py`; drop 2 unused `type: ignore`. Verify `mypy --strict`
      clean, pytest green (343 passed / 5 skipped), ruff clean.
- [x] T-3 (REQ-003) Delete empty `arctrust/tests/__init__.py` and `arccli/tests/__init__.py`.
      Verify per-package pytest green and cross-package `--import-mode=importlib` collects.
- [x] T-4 (REQ-004) TDD: add per-tier default ceiling constants + `_effective_ceilings` in
      `tools/_policy_fill.py`; route both resolvers through it. RED→GREEN in
      `tests/unit/tools/test_policy_fill.py` (federal enforced, personal unbounded).
- [x] T-5 (collateral) Fix stale `test_federal_lists_all_five` layer assertion (add
      `classification`) and rename.

## Verification
- [x] `check_loc_budgets.py` → PASS (core 3411).
- [x] arcagent pytest 3442 passed / 16 skipped; mypy strict clean (257 files); ruff clean.
- [x] arcskill pytest 343 passed / 5 skipped; mypy strict clean; ruff clean.
- [x] arccli pytest 352 passed; mypy/ruff clean.
- [x] arctrust pytest 358 passed; mypy/ruff clean.
- [x] Cross: `pytest arctrust arcagent arccli --import-mode=importlib` collects + green.
</content>
