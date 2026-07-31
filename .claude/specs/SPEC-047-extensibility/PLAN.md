# SPEC-047 — Extensibility + Blueprints Framework · PLAN

**Status:** COMPLETE
**Branch:** `feat/SPEC-047-extensibility`
**Approach:** TDD (RED → GREEN → REFACTOR). Every phase ends `ruff` + `mypy --strict` clean, full
package matrix green, arcagent **core** NCLOC < 3500. Behavior-preserving refactor proven before any
new surface is added. Every mechanism driven through the **real production path** (producers-unwired
defense — this program has caught unwired producers in 6 consecutive specs).

Legend: `[ ]` pending · `[x]` done. Pillar tags S/Mo/Se/Sc. Each task names its AC.

---

## Phase 0 — Baseline & guardrails
- [x] **T0.1** Capture baselines: arcagent core NCLOC 3498/3500; `brain/` 5 tests, `skilladapt/` 10
  tests green. Recorded in README "Baseline". *(Se/S)*
- [x] **T0.2** Call sites: `modules/memory/_runtime.py:66` (`select_brain`),
  `modules/skills/_runtime.py:108` (`select_skill_adapter`) — both keyword-only, UNCHANGED. Only
  mechanical test edit: `tests/unit/brain/test_select.py` `_patch_import` repoints
  `select.importlib` → `arcagent.extension.select.importlib` (the BYO import moved). *(Mo)*
- [x] **T0.3** No architecture test allowlists top-level `arcagent/` packages (`test_layering.py`
  governs only package↔package boundaries). Adding `extension`/`blueprints`/`tiers` needs no
  allowlist edit. *(Mo)*

## Phase 1 — Generalized select-one mechanism (dedup, behavior-preserving)
- [x] **T1.1 (RED)** `tests/unit/extension/test_select.py` — 13 tests over the dispatch table
  (none/builtin/auto/BYO × personal/enterprise/federal, warn-vs-silent, refuse-before-import). *(Se)*
- [x] **T1.2 (GREEN)** `extension/point.py` (`ExtensionPoint`) + `extension/select.py`
  (`select_extension`, `_try_builtin`, `_load_byo`). No static import of any builtin/BYO package.
  *(S/Mo/Se)* → REQ-001,002,005
- [x] **T1.3 (REFACTOR)** `brain/select.py` = `_BRAIN_POINT` + thin `select_brain`; deleted its
  choice dispatch, `_load_custom`, inline BYO gate. `_build_arcmemory(module,ctx)` builder;
  `_build_embedder`/`_build_distiller` unchanged. Public signature unchanged. *(Mo)* → REQ-003
- [x] **T1.4 (REFACTOR)** `skilladapt/select.py` = `_SKILLADAPT_POINT` + thin
  `select_skill_adapter`; `_build_arcskill(module,ctx)` builder. *(Mo)* → REQ-003
- [x] **T1.5 (VERIFY)** brain (5) + skilladapt (10) suites pass unchanged bar the one enumerated
  import-patch edit. *(S)* → **AC-1**, REQ-004
- [x] **T1.6 (SECURITY E2E)** `tests/security/test_byo_refuse_before_import.py` — enterprise + federal
  BYO brain + adapter through the real `_runtime.configure`, filesystem import sentinel un-fired. *(Se)*
  → **AC-2**, REQ-002

## Phase 2 — Family registry + inspection
- [x] **T2.1 (RED)** `tests/unit/extension/test_families.py` — 4 families + correct kinds;
  `inspect_extensions` select-one over a real `ArcAgentConfig` (none/builtin/BYO-refused/allowlisted)
  and scan-many tools/hook-builds over a real `CapabilityRegistry`. *(Mo)*
- [x] **T2.2 (GREEN)** `extension/families.py` (`SelectOneFamily`/`ScanManyFamily`, 4-family
  `FAMILIES`; scan-many filtered by decorator kind) + `extension/inspect.py` (`ExtensionStatus`,
  `inspect_extensions`; builtin probed with `find_spec` — no import; BYO judged by allowlist gate,
  never imported). *(S/Mo)* → REQ-006,030,031

## Phase 3 — Tier-relaxation surface
- [x] **T3.1 (RED)** `tests/unit/test_tiers.py` — 16 tests: tier stringency order; exact floor
  force/reject; smaller pin/reject-disabled/reject-looser/honor-stricter; personal/enterprise
  relaxation + audit; non-relaxable → raise. *(Se)*
- [x] **T3.2 (GREEN)** `arcagent/tiers.py` (`RelaxableKnob`, `RELAXABLE_KNOBS` incl. reference rows,
  `SECURITY_CONFIG_KNOBS`, `resolve_tier_floor`, `tier_rank`/`stricter_tier`, audit-on-relaxation).
  *(S/Se)* → REQ-020,021,023
- [x] **T3.3 (REFACTOR)** DELEGATED: `SecurityConfig._enforce_tier_crypto_floor` now loops
  `SECURITY_CONFIG_KNOBS` → `resolve_tier_floor`; deleted `_reject_weaker_federal_override` +
  `_apply_federal_breaker_floors` + the two module floor constants. SPEC-037/043 tests pass
  unchanged. **Core NCLOC 3498 → 3463 (−35, matches OQ-5 estimate).** *(Se/S)* → REQ-021,022, **AC-4 (partial)**

## Phase 4 — Blueprints
- [x] **T4.1 (RED)** `tests/unit/blueprints/test_loader.py` — 16 tests: parse a `[blueprint]` TOML;
  merge precedence (user value wins over blueprint); stringency-max effective tier (personal deployment
  + federal blueprint → federal; federal deployment + personal blueprint → federal); denied-key strip;
  unsigned user blueprint above personal → refused; signed → applies; tampered → refused; dumps_toml
  round-trips through the real `ArcAgentConfig`. *(Se)*
- [x] **T4.2 (GREEN)** `blueprints/loader.py` (`resolve_blueprint`, `apply_blueprint`, `dumps_toml`,
  `list_blueprints`, tier-floor guard) reusing `capabilities/artifact_signing.verify_file`/`load_signature`
  + `tiers.stricter_tier`/`tier_rank` + `core.config._deep_merge`. *(S/Se)* → REQ-010–014
- [x] **T4.3 (GREEN)** 3 packaged blueprints (`personal-assistant`, `enterprise-ops`, `federal-analyst`)
  under `arcagent/blueprints/`. **DEVIATION from SDD §4.1:** `brain`/`adapter` nest under
  `[modules.<m>.config]` (the real `mod_entry.config` shape) — the SDD example put them directly under
  `[modules.<m>]`, which `ModuleEntry` ignores as extra → would select NOTHING (producers-unwired trap).
  Ship in wheel via hatchling default file inclusion under `src/arcagent`. *(S)* → REQ-010
- [x] **T4.4 (GREEN)** Apply-time audit: emit `blueprint.applied` in the CLI apply path (P5), inside the
  `if not dry_run` write guard so a `--dry-run` writes nothing and audits nothing (MED-2 fix, driven by
  `test_dry_run_emits_no_audit_record`). *(Se)* → REQ-015, **AC-7**
- [x] **T4.5 (SECURITY)** Federal deployment + personal blueprint → `SecurityConfig(**merged["security"])`
  (real model_validator) forces FIPS/vault_transit/ecdsa. Full flat-load boot is AC-4 E2E in P6. *(Se)*
  → **AC-4**, REQ-013
- [x] **T4.6 (SECURITY)** Unsigned/tampered/**wrong-key** user blueprint above personal → `resolve_blueprint`
  raises before any merge/write; verification is PINNED to the deployment operator's key and an
  unresolvable operator key denies fail-closed (HIGH-1 fix). *(Se)* → **AC-5**, REQ-014

## Phase 5 — arccli surface
- [x] **T5.1 (GREEN)** `commands/blueprint.py`: `list` / `show` / `apply [--agent] [--dry-run]` /
  `verify` / `sign`, + reusable `apply_to_disk` / `audit_apply` core (deep-merge under existing config,
  preserve identity, WORM-or-log audit). *(S)* → REQ-011,015,016,017
- [x] **T5.2 (GREEN)** **DEVIATION (OQ-8 LOCKED):** folded inspection INTO the existing `arc ext`
  (`ext inspect` / `ext verify`) — NOT a colliding new `commands/extensions.py`. Renders
  `inspect_extensions` over a flat-read config + a real `CapabilityRegistry`. *(S)* → REQ-030,031
- [x] **T5.3 (GREEN)** `commands/init.py`: full `open`→`personal` rename (all CLI + all 3 generated
  files incl. the arcllm.toml leak; `open` fully removed, no alias). `--blueprint` flag → resolve +
  verify + deep-merge UNDER init defaults (dict-based arcagent gen via `dumps_toml`); stringency-max
  tier; apply audit via shared `audit_apply`. *(S/Se)* → REQ-011,012,013
- [x] **T5.4 (GREEN)** Register the `blueprint` `CommandDef` (lazy handler) in `COMMAND_REGISTRY`; no
  `extensions` CommandDef (folded into `ext` per T5.2). *(Mo)* → REQ-032

## Phase 6 — End-to-end producers-unwired defense
- [x] **T6.1 (E2E)** `tests/integration/test_blueprint_boot_e2e.py`: materialize `personal-assistant`
  → **real `__main__._load_config` FLAT read** (NOT `load_config`, per DC-8b) → real `ArcAgent.startup`
  → assert the **concrete** `ArcMemoryBrain` is active (arcmemory installed) + `state().active`. No stub.
  *(Se/S)* → **AC-3**, REQ-011,041
- [x] **T6.2 (E2E)** Same file: boot the agent, run `inspect_extensions(config, agent._capability_registry)`
  over the LIVE registry; assert the brain row equals the agent's real selected brain and builtin tools
  surface as scan_many rows. *(S)* → **AC-6**, REQ-030,041
- [x] **T6.3 (E2E)** `arccli tests/test_cli_blueprint.py::test_apply_relaxation_audit_fires_at_enterprise`
  drives the real `apply_to_disk` path: a signed enterprise blueprint relaxing `runaway_max_repeat` emits
  BOTH `tier.relaxation_granted` (with `knob`) and `blueprint.applied` (with fields). *(Se)* → **AC-7**,
  REQ-015,023

## Phase 7 — Quality, docs, versions
- [x] **T7.1** Coverage (new modules): blueprints 88% · extension 93–100% · tiers **96%** — extension +
  tiers (policy-adjacent) ≥ 90% met; overall new-module coverage 92.9%. *(S)*
- [x] **T7.2** `ruff check` + `mypy --strict` clean across arcagent + arccli. **Fixed pre-existing debt
  surfaced:** `core/config.py` + a test had unsorted import blocks (leave-it-correct). *(S)*
- [x] **T7.3** arcagent **core** NCLOC **3463 / 3500** (37 remaining; all new code outside core). Net
  spec LOC well under ~600; seam-selection LOC decreased (P1 dedup). *(S)* → REQ-040
- [x] **T7.4** Docstrings/READMEs → reality; version bumps arcagent 0.14.0→**0.15.0**, arccli
  0.5.1→**0.6.0** (arccli `__version__` now the importlib.metadata pattern, fallback literal);
  CHANGELOGs updated. Packaged `.toml` presets **verified shipping in the built wheel**. *(S)*
- [x] **T7.5** README status → COMPLETE. *(S)*

## Full package matrix (fresh, T7)
arcagent **2866 passed** / 16 skipped · arccli **365 passed** · arcgateway **585 passed**. ruff + mypy
--strict clean both packages. core LOC 3463/3500. Blueprints ship in the wheel (verified).

---

## Acceptance-criteria roll-up

| AC | Proven by | Real-path guarantee |
|---|---|---|
| AC-1 behavior-preserving refactor | T1.5 | pre-existing suites, only enumerated import edits |
| AC-2 BYO refuse-before-import | T1.6 | through `_runtime.configure`, import sentinel |
| AC-3 blueprint boots selected brain | T6.1 | `arc init` → `load_config` → `ArcAgent.startup` → concrete Brain |
| AC-4 federal floor holds through blueprint | T3.3 + T4.5 | real `SecurityConfig.model_validator` |
| AC-5 unsigned blueprint refused above personal | T4.6 | real `resolve_blueprint` verify gate |
| AC-6 `arc extensions` reflects reality | T6.2 | live config + real `CapabilityRegistry` |
| AC-7 audit on apply + relaxation | T4.4 + T6.3 | real WORM/telemetry sink emission |

## Definition of done
- All phases checked; ACs proven with fresh output.
- arcagent core NCLOC < 3500; net spec LOC ≤ ~600 with seam-selection LOC down.
- `ruff` + `mypy --strict` + full package matrix green.
- Existing brain/skilladapt/capability suites unchanged (bar enumerated import edits).
- Four Pillars on blueprints (identity/sign/authorize/audit) demonstrated.
- README OQs resolved or explicitly deferred with Josh sign-off.
