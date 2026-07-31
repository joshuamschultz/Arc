# Phase 0 — Baseline Inventory

> Captured **2026-05-07** to satisfy §3 exit criterion: every pre-existing failure recorded with file:line. Per CLAUDE.md "fix as we encounter them" — these are the inherited failures Phase 1+ work should resolve when touching the relevant files.
>
> **Update 2026-05-07 (post-quick-wins):** Clusters A/B/C/E/F resolved before Phase 1 dispatch. Remaining open: cluster D (arcagent spawn-e2e, 3 failures) + bulk mypy (~209 errors across arcllm/arcrun/arcskill/arctui/arcui).

## Tool matrix

| Package    | Project name | ruff | mypy --strict | pytest |
|------------|--------------|------|---------------|--------|
| arcagent   | arc-agent    | ✅    | ❌ 1 (early-fail; module dup) | ❌ 3 fail / 3379 pass |
| arccli     | arccmd       | ✅    | ❌ 4 in 2 files               | ✅ 323 pass |
| arcgateway | arcgateway   | ✅    | ✅ 33 files clean              | ✅ 747 pass / 1 skip |
| arcllm     | arcllm       | ✅    | ❌ 46 in 25 files              | ❌ 13 fail / 894 pass |
| arcmas     | arcmas       | ✅    | ✅ 1 file clean                | ⚠ no tests dir |
| arcmodel   | arcmodel     | ✅    | ✅ 1 file clean                | ⚠ no tests dir |
| arcprompt  | arcprompt    | ✅    | ✅ 1 file clean                | ⚠ no tests dir |
| arcrun     | arcrun       | ✅    | ❌ 28 in 11 files              | ✅ 398 pass / 3 skip |
| arcskill   | arcskill     | ✅    | ❌ 35 in 14 files              | ✅ 342 pass / 5 skip |
| arcteam    | arcteam      | ✅    | ❌ 1                          | ✅ 331 pass |
| arctrust   | arctrust     | ✅    | ✅ 6 files clean               | ✅ 176 pass |
| arctui     | arctui       | ✅    | ❌ 71 in 20 files              | ⚠ no tests dir |
| arcui      | arcui        | ✅    | ❌ 28 in 11 files              | ❌ 7 fail / 675 pass / 7 skip |

**Totals (initial baseline):** ruff 13/13 clean; mypy 5/13 clean (~214 pre-existing strict errors); pytest 8/13 green (23 pre-existing failures). 4/13 have no `tests/` directory.

**Totals (post-quick-wins):** ruff 13/13 clean; mypy **7/13** clean (~209 errors remaining); pytest **11/13** green (3 pre-existing failures remaining — cluster D only). 4/13 still have no `tests/` directory.

> Note: §3 of the plan listed all 13 as `packages/<dir>` but uv workspace project names diverge from dir names for `arcagent` (`arc-agent`) and `arccli` (`arccmd`). Inventory uses dir name throughout; uv invocations use project name.

## Pre-existing pytest failures

### A. `arcllm/tests/test_aws_secrets_backend.py` — 13 failures (missing optional dep) — **RESOLVED**
> Fix: added `pytest.importorskip("boto3")` + `importorskip("botocore")` at module load. The 13 individual tests collapse into 1 module-level skip when the optional deps aren't installed; pytest run now: 893 pass / 1 skip / 0 fail.
- **Root cause:** `ModuleNotFoundError: No module named 'boto3'` / `'botocore'`. The AWS Secrets backend has an optional dependency that isn't in dev deps.
- **Failing tests** (all in this file):
  - `TestProtocolConformance::test_backend_satisfies_vault_protocol`
  - `TestHappyPath::test_get_secret_returns_string_value`
  - `TestHappyPath::test_get_secret_decodes_secret_binary`
  - `TestHappyPath::test_is_available_true_when_client_constructed`
  - `TestErrorSemantics::test_resource_not_found_returns_none`
  - `TestErrorSemantics::test_access_denied_raises_config_error`
  - `TestErrorSemantics::test_no_credentials_makes_backend_unavailable`
  - `TestErrorSemantics::test_generic_client_error_returns_none_marks_unavailable`
  - `TestRegionAndProfile::test_region_defaults_to_boto3_chain`
  - `TestRegionAndProfile::test_explicit_region_passed_through`
  - `TestRegionAndProfile::test_profile_uses_session`
  - `TestNoSecretsInRepr::test_repr_never_includes_fetched_secret`
  - `TestConstructorAcceptsKwargs::test_kwargs_only_init_does_not_raise`
- **Resolution path:** add `boto3`/`botocore` to arcllm test deps, or guard tests with `pytest.importorskip("boto3")`.

### B. `arcui/tests/unit/test_setup_vm_manifest.py` — 5 failures (missing shell file) — **RESOLVED**
> Fix: deleted `packages/arcui/tests/unit/test_setup_vm_manifest.py`. The `deploy/lib/agent-manifest.sh` lib was intentionally removed in commit `ff5270d` ("make repo public-safe — keep only the main agent framework"); the tests are stranded references to a private deploy infra that's no longer in this repo.
- **Root cause:** tests source `/Users/joshschultz/Projects/arc/deploy/lib/agent-manifest.sh` which does not exist on disk. `set -u; . agent-manifest.sh; _read_agent_manifest` returns 127.
- **Failing tests:**
  - `test_manifest_with_well_formed_entries_passes` (`packages/arcui/tests/unit/test_setup_vm_manifest.py:45`)
  - `test_manifest_with_path_traversal_aborts` (`:54`)
  - `test_manifest_with_uppercase_entry_aborts` (`:61`)
  - `test_manifest_with_shell_metachar_aborts` (`:68`)
  - `test_manifest_missing_falls_back_to_default` (`:74`)
  - `test_manifest_empty_aborts` (`:83`)
- **Resolution path:** restore `deploy/lib/agent-manifest.sh` (likely lost in a deploy-script reorganization) or delete tests if the function moved.

### C. `arcui/tests/integration/test_self_hosted_fonts.py::test_fonts_css_declares_inter_and_jetbrains_mono` — 1 failure (test stale vs implementation) — **RESOLVED**
> Fix: removed `test_fonts_css_declares_inter_and_jetbrains_mono`. `fonts.css` is now an intentional comment-only placeholder (the WOFF2 binaries were never committed; system-font fallbacks are the documented current state with restoration steps in the README). The remaining 3 fonts tests cover CDN-absence, file-reachability, and README-presence.
- **Root cause:** `arcui/static/assets/fonts.css` was rewritten to "currently relying on system fallbacks" and intentionally drops `font-family: "Inter"` / `JetBrains Mono` declarations. Test asserts the old behavior.
- **Resolution path:** decide which is canonical — the css comment claims system-fallback is intentional for air-gapped deploys, so likely the test is stale.

### D. `arcagent/tests/integration/orchestration/test_spawn_e2e.py` — 3 failures (`*_real_llm`) — **OPEN**
> Not addressed in quick-wins. Will be triaged when Phase 4 touches `arcagent/orchestration/spawn.py` (which the plan splits in §8.2).
- **Root cause:** Tests assert `spawn.start` event is emitted; observed event sequence omits it. Either the spawn instrumentation moved to a different event name or these tests require a live LLM that didn't actually call the spawn tool in this run.
- **Failing tests:**
  - `test_single_spawn_real_llm` (`:155`)
  - `test_parallel_spawns_real_llm` (`:212`)
  - `test_spawn_audit_trail_real_llm` (`:287`)
- **Resolution path:** if `_real_llm` means "needs ANTHROPIC_API_KEY" then env-gate. If event names changed, update assertions.

## Pre-existing mypy --strict errors (top of list — full detail in `baseline-mypy.log`)

### `arcagent` — 1 error (early-fail)
- `packages/arcagent/src/arcagent/builtins/capabilities/skills/create-tool/scripts/validate.py` — Duplicate module named `validate` (also at `create-skill/scripts/validate.py`). Mypy can't continue past this collision, so the real total is unknown.
- **Resolution path:** rename one of the two `validate.py` modules, or add `__init__.py` shims, or exclude these script-files from mypy.

### `arccli` — 4 errors in 2 files — **RESOLVED**
- `arccli/main.py:65` — Unused `# type: ignore` comment (`[unused-ignore]`) → fixed (annotation added, ignore removed)
- `arccli/main.py:65` — Function missing return type annotation → fixed (`-> Completer | None` via `TYPE_CHECKING`-guarded import)
- `arccli/main.py:90` — Call to untyped function `_build_completer` → fixed (function now typed)
- `arccli/__main__.py:3` — `Module "arccli.main" has no attribute "cli"` → fixed (legacy Click entry-point name; updated to `from arccli.main import main; main()`)
> Note: fixing the `attr-defined` error unblocked mypy from following imports transitively, surfacing 29 already-counted errors in arcllm/arcrun. Those are tracked in their own per-package totals, not double-counted here.

### `arcteam` — 1 error — **RESOLVED**
- `packages/arcteam/src/arcteam/storage.py:206` — `Returning Any from function declared to return "int"` → fixed (`isinstance(seq, int)` guard before return).

### `arcllm` — 46 errors in 25 files | `arcrun` — 28 in 11 | `arcskill` — 35 in 14 | `arctui` — 71 in 20 | `arcui` — 28 in 11
See raw `baseline-mypy.log` for line-level detail. These will be addressed file-by-file as Phases 1–6 touch the relevant code.

## `pytest --collect-only` (workspace-root)

`uv run pytest -q --collect-only` from repo root: **3930 tests collected, 113 collection errors in 5.02s**. The collection errors are sys.path / conftest issues that surface only at root level — per-package pytest (the canonical baseline) ran 4286 tests with 23 failures. The 113-error root count is captured in `baseline-collect.log` for §12 future-diff use only; the per-package log is the official baseline.

## Phase 0 exit criterion — met

> §3: "every package green on all three tools, *or* every existing failure recorded as 'pre-existing' with file:line so we know what we inherited."

Inventory documents every pre-existing failure with file:line citations. Recorded path is satisfied. Phase 0 is complete.

Subsequent phases follow CLAUDE.md "fix as we encounter them" — when a Phase 1+ task touches a file with baseline errors above, fix the errors as part of that task's diff.

## Quick-win results

| # | Cluster | Status | Effect |
|---|---------|--------|--------|
| A | arcllm `boto3` deps | ✅ resolved | 13 failures → 1 module-level skip when optional deps absent |
| B | arcui `agent-manifest.sh` | ✅ resolved | test file deleted; 5 failures gone; deploy infra intentionally removed in `ff5270d` |
| C | arcui fonts test | ✅ resolved | 1 failure gone; 3 sibling fonts tests retained |
| D | arcagent spawn-e2e | ⏭ deferred | will fix when Phase 4 splits `spawn.py` per §8.2 |
| E | arccli main.py 4-error cluster | ✅ resolved | mypy 4 → 0 in arccli's own files |
| F | arcteam storage.py:206 | ✅ resolved | mypy 1 → 0 |

**Net effect:** pytest 23 → 3 failures; mypy 214 → ~209 errors; ruff 0 → 0 (still clean); 6 stranded test functions removed.

## Status: Phase 0 closed. Ready for Phase 1.
