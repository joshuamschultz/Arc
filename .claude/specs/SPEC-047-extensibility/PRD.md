# SPEC-047 — Extensibility + Blueprints Framework · PRD

**Feature:** Make **EXTENSIBLE** a first-class, product-visible property of Arc. Generalize the two
delivered select-one extension seams (Brain from SPEC-041, SkillAdapter from SPEC-044) into **one
consistent extension-point mechanism** that new extension points *declare* rather than re-implement;
add **signed, versioned preset-config blueprints** for one-command bootstrap; and give
**config-relaxable tiers** a single declared enforcement surface (federal floors non-relaxable,
audit on relaxation). Surface all of it through arccli (`arc extensions`, `arc blueprint`,
`arc init --blueprint`).

**Type:** Framework generalization (dedup + WIRE) + preset-config system + CLI surface
**Status:** DRAFT (spec)
**Branch:** `feat/SPEC-047-extensibility`
**Phase:** Phase 3 — end-goal emphasis (`ROADMAP-PROGRAM.md`)
**Depends on / references:** SPEC-041 (Brain seam + BYO signing gate), SPEC-044 (SkillAdapter seam),
SPEC-021 (capability loader + `@tool`/`@hook`/`@background_task`/`@capability` decorators),
SPEC-033 (sidecar sign/verify), SPEC-037 (federal crypto-floor `model_validator`), SPEC-043 (tier
HITL ladder + breaker floors), SPEC-053 (operator-key audit authority), ADR-019 (tier = stringency,
not a gate).
**Confidence:** High (both precedents in-tree; roadmap scope explicit; mostly WIRE + dedup + CLI).

---

## 1. Problem & context

Arc has two production extension seams that are **byte-for-byte structurally identical** in their
selection and security logic, yet each re-implements it:

| Seam | File | Shape |
|---|---|---|
| Brain / retrieval (SPEC-041) | `arcagent/brain/select.py` | Protocol + `NullBrain` default + config-select (`none`/`arcmemory`/`auto`/dotted-BYO) + lazy importlib + unsigned-BYO refused-before-import above personal |
| Skills (SPEC-044) | `arcagent/skilladapt/select.py` | Protocol + `NullSkillAdapter` default + config-select (`none`/`arcskill`/dotted-BYO) + identical BYO allowlist gate |

The **choice dispatch** (`none`/builtin/`auto`/dotted-path), the **BYO fail-closed allowlist gate**
(`tier != personal and class_path not in allowlist → refuse before import`), and the **`_load_custom`
dotted-path importer** are duplicated across the two files. A third extension point would copy them a
third time. This is the classic pattern-of-three: the mechanism must be extracted so new extension
points **declare** an `ExtensionPoint` and inherit the exact security semantics for free.

Separately, three product gaps block "install arcagent, pick a blueprint, add capability packages":

1. **No blueprints.** A new adopter must hand-assemble tier + extension selections + module config +
   policy. There is no named, signed, versioned preset ("personal-assistant", "federal-analyst",
   "enterprise-ops") that bootstraps a coherent deployment in one command.
2. **Tier stringency is enforced ad-hoc.** `SecurityConfig._enforce_tier_crypto_floor` (SPEC-037),
   `_apply_federal_breaker_floors` (SPEC-043), `resolve_workspace_import_policy` (capabilities),
   `BudgetConfig` dispatch-time resolution, and skilladapt change-bound floors each hand-roll the same
   "personal relaxable / federal floor non-relaxable" idiom. There is no single declared surface of
   *which knob is relaxable at which tier* and no uniform **audit on relaxation**.
3. **Extensions are invisible.** There is no way to ask a deployment "what is selected, what is
   available, what is signed" across the four extension-point families the roadmap names:
   **brain/retrieval, skills, tools, hook-builds**.

## 2. The four extension-point families (investigated)

Investigation of the codebase shows Arc has exactly **two extension shapes**, and the four named
families map onto them cleanly:

| Family | Shape | Mechanism today | SPEC-047 action |
|---|---|---|---|
| **brain/retrieval** | select-one | `brain/select.py` (Protocol+Null+select+BYO gate) | Becomes an `ExtensionPoint` instance of the generalized mechanism |
| **skills** | select-one | `skilladapt/select.py` (same) | Becomes an `ExtensionPoint` instance |
| **tools** | scan-many | `@tool` capabilities discovered by `CapabilityLoader` from 4 scan roots (SPEC-021), `.arcsig`-verified at load (SPEC-033), bridged to `ToolRegistry` | Declared as a scan-many family; exposed via `arc extensions` (no rebuild — the loader already generalizes it) |
| **hook-builds** | scan-many | `@hook` / `@background_task` / `@capability` capabilities loaded by the *same* `CapabilityLoader`, bridged to `ModuleBus` + background scheduler | Declared as a scan-many family; exposed via `arc extensions` (same loader) |

"hook-builds" = the family of loadable **bus-hook / background-task / lifecycle-class** capabilities
(as distinct from the callable-surface `@tool` family). Both are the same loader keyed by decorator
`kind`. **SPEC-047 does not build a new plugin system** — it declares the two existing shapes as
first-class families, unifies the select-one duplication, and adds inspection + blueprints on top.

## 3. Goals / non-goals

### Goals
- One generalized select-one mechanism (`Protocol + Null + select + signed-BYO gate`) that new
  extension points declare; `brain/select.py` and `skilladapt/select.py` become thin instances with
  the duplicated dispatch/gate **deleted** (net seam LOC down; behavior preserved).
- Named, versioned, **signed** blueprints; `arc init --blueprint X` and `arc blueprint apply` bootstrap
  a deployment; defined merge precedence; **tier floors non-relaxable through a blueprint**.
- A single declared **tier-relaxation surface**: which knobs relax at which tier, federal floors
  non-relaxable, one shared helper the existing enforcement points and blueprint-apply consult, audit
  on every granted relaxation.
- `arc extensions` inspection across all four families (selected / available / signed).

### Non-goals
- No new plugin/loader engine — the SPEC-021 `CapabilityLoader` is the scan-many mechanism, unchanged.
- No new crypto — blueprints reuse arctrust `ArtifactSignature` + the SPEC-033 `.arcsig` sidecar.
- No change to the Brain / SkillAdapter Protocols or the arcmemory / arcskill packages.
- No arcagent **core** growth — arcagent core is frozen at 3498/3500 NCLOC; all new code lands in
  `arcagent/extension/`, `arcagent/blueprints/`, `arcagent/tiers.py` (siblings of `core/`, like
  `brain/` and `skilladapt/`) or in arccli.

## 4. Users & use cases

- **New adopter (personal):** `arc init --blueprint personal-assistant` → a working agent with memory
  on (arcmemory) and skills-improver on, zero further config.
- **Enterprise operator:** `arc blueprint apply enterprise-ops --agent ./team/ops` → tier=enterprise,
  audit + budgets + signed capabilities required, in one audited step.
- **Federal integrator (SCIF):** `arc init --blueprint federal-analyst` → tier=federal with FIPS /
  vault_transit / signed-everything floors that **no blueprint can weaken**.
- **Extension author:** writes a BYO Brain or SkillAdapter, has the operator allowlist/sign it, and
  `arc extensions verify` confirms it is trusted before an enterprise/federal agent will load it.
- **Auditor / reviewer:** `arc extensions` shows exactly what a deployment has plugged in and whether
  each artifact is signed — a compliance-legible inventory.

---

## 5. Requirements (EARS format)

MoSCoW: **M**=Must, **S**=Should, **C**=Could. Pillar: S=Simplicity, Mo=Modularity, Se=Security,
Sc=Scalability.

### 5.1 Generalized extension-point mechanism

- **REQ-001 (M, S/Mo):** The system SHALL provide one `ExtensionPoint` descriptor and one
  `select_extension()` function that together implement the select-one shape (Null default, `none`
  → Null, builtin name → lazy import + build, `auto` → builtin-if-importable-else-Null, dotted path →
  BYO) for any extension point.
- **REQ-002 (M, Se):** When a dotted-path BYO implementation is selected AND the tier is not
  `personal`, the system SHALL refuse it **before importing the module** unless its class-path is on
  the operator allowlist, raising a fail-closed error. *(Preserves the exact SPEC-041/044 semantic —
  importing an unverified class-path is startup RCE, ASI04.)*
- **REQ-003 (M, Mo):** `arcagent/brain/select.py` and `arcagent/skilladapt/select.py` SHALL be
  re-expressed as `ExtensionPoint` instances calling `select_extension()`, with the duplicated choice
  dispatch, BYO gate, and `_load_custom` importer **deleted** (no-legacy). The builtin-specific
  builders (`_try_arcmemory`, `_try_arcskill`) remain, unchanged in behavior.
- **REQ-004 (M, S):** The refactor SHALL be behavior-preserving: the existing `brain/` and
  `skilladapt/` unit + security tests pass unchanged, except mechanical import-path updates which
  SHALL be enumerated in the PLAN.
- **REQ-005 (M, Se):** `select_extension()` SHALL never statically import a builtin extension package
  (arcmemory, arcskill, or any BYO module); the only import path SHALL be the lazy, guarded one inside
  the builtin builder / BYO loader, so `pip install arcagent` alone still boots (Null defaults).
- **REQ-006 (S, Mo):** The four families (brain, skills, tools, hook-builds) SHALL be declared in one
  place as a family registry, each carrying its kind (select-one / scan-many) and how to read its
  current state.

### 5.2 Blueprints

- **REQ-010 (M, S):** The system SHALL define a blueprint as a TOML document with a `[blueprint]`
  header (`name`, `version`, `tier`, `description`) plus arcagent-config overlay sections; official
  blueprints (`personal-assistant`, `enterprise-ops`, `federal-analyst`) SHALL ship packaged with
  arcagent.
- **REQ-011 (M, S):** `arc init --blueprint <name>` SHALL resolve, verify, and merge a blueprint into
  the generated config in one command; `arc blueprint apply <name> [--agent DIR]` SHALL apply a
  blueprint to an existing deployment.
- **REQ-012 (M, S):** Blueprint merge precedence SHALL be **packaged defaults < blueprint < user
  `~/.arc` config < per-agent file < env vars** — a user's explicit config always wins over a
  blueprint (the blueprint is a *starting point*, not an override).
- **REQ-013 (M, Se):** A blueprint SHALL NOT be able to weaken a tier floor. The effective tier SHALL
  be the **stringency-max** of the deployment's configured tier and the blueprint's declared tier
  (federal > enterprise > personal), and the standard `SecurityConfig` tier validators SHALL run on
  the merged result. *When a `federal` deployment applies a `personal` blueprint, the result stays
  federal with all federal floors intact.*
- **REQ-014 (M, Se):** Every blueprint SHALL carry an arctrust `ArtifactSignature` (`.arcsig`
  sidecar). `arc blueprint apply` / `arc init --blueprint` SHALL **verify the signature before use**;
  above the `personal` tier an unsigned or signature-invalid blueprint SHALL be refused (fail-closed),
  mirroring the capability-loader signature floor.
- **REQ-015 (M, Se):** Applying a blueprint SHALL emit an audit event recording `name`, `version`,
  content digest, signer DID, and the resulting effective tier — to the operator-signed WORM sink when
  one is configured, else to telemetry (fail-open on audit-sink setup, never blocking apply on a sink
  error but never silently dropping the record when a sink exists).
- **REQ-016 (S, S):** `arc blueprint list` SHALL list packaged + user (`~/.arc/blueprints/`)
  blueprints with name, version, tier, and signed-status; `arc blueprint show <name>` SHALL print the
  resolved overlay; `arc blueprint verify <name>` SHALL report signature validity.
- **REQ-017 (C, Se):** `arc blueprint sign <path>` SHALL let an operator sign a user-authored
  blueprint with the operator/author key, producing its `.arcsig` sidecar (reusing SPEC-033 signing).

### 5.3 Config-relaxable tiers

- **REQ-020 (M, Se):** The system SHALL declare, in one place, the set of tier-relaxable knobs, each
  with its federal floor and whether `personal`/`enterprise` may relax it (a `RelaxableKnob` table).
- **REQ-021 (M, Se):** The system SHALL provide one shared helper (`resolve_tier_floor` / analogous)
  that enforces "explicit weaker-than-floor value at a tier that forbids relaxation → fail closed";
  the existing federal-crypto-floor and federal-breaker-floor logic in `SecurityConfig` SHALL delegate
  to it (dedup), provided arcagent **core** NCLOC stays under 3500 after the change — else additive-only
  (see OQ-5).
- **REQ-022 (M, Se):** Federal floors SHALL be non-relaxable by any means (config, blueprint, or env);
  a federal deployment that leaves a floored knob unset SHALL be pinned to the floor, and one that sets
  a looser/disabled value SHALL fail closed. *(Preserves SPEC-037/043 behavior.)*
- **REQ-023 (S, Se):** When `personal`/`enterprise` grants a relaxation (loosens a default the floor
  permits loosening), the system SHALL emit an audit event naming the knob, tier, requested value, and
  resolved value.

### 5.4 arccli extension inspection

- **REQ-030 (M, S):** `arc extensions [list]` SHALL show, for each of the four families: family name,
  kind (select-one / scan-many), what is selected/loaded, whether the selection is available
  (importable / discoverable), and its signed status.
- **REQ-031 (S, Se):** `arc extensions verify` SHALL check that every above-personal BYO selection is
  allowlisted, and that discovered scan-many capabilities carry valid signatures where the tier
  requires them, reporting any that would be refused at load.
- **REQ-032 (M, Mo):** The `blueprint` and `extensions` commands SHALL be registered in the arccli
  `COMMAND_REGISTRY` (single source of truth) and follow the existing lazy-handler convention.

### 5.5 Cross-cutting

- **REQ-040 (M, S):** All new arcagent code SHALL live outside `arcagent/core/`; arcagent core NCLOC
  SHALL remain < 3500 and the full package matrix SHALL be `ruff` + `mypy --strict` clean.
- **REQ-041 (M, Se):** Every mechanism SHALL be proven end-to-end through the **real production path**
  (see PLAN acceptance criteria), never a rigged fixture — the standing producers-unwired defense.

---

## 6. Acceptance criteria (headline, EARS)

- **AC-1 (REQ-003/004):** WHEN the seams are refactored, the pre-existing `brain/` and `skilladapt/`
  test suites pass with only enumerated mechanical import edits, and `select_brain` / `select_skill_adapter`
  keep identical public signatures.
- **AC-2 (REQ-002):** WHEN an enterprise or federal agent selects an unsigned/non-allowlisted dotted
  BYO brain or adapter, THEN startup fails closed **and the BYO module is never imported** (asserted via
  an import-side-effect probe), through the real `select_extension` path.
- **AC-3 (REQ-011/012):** WHEN `arc init --blueprint personal-assistant` runs, THEN the written config
  merges the blueprint under any user override, and a real `ArcAgent` boots with the blueprint's
  selected brain active (asserted by the concrete selected Brain type, not a stub).
- **AC-4 (REQ-013):** WHEN a `federal` deployment applies a blueprint declaring `tier = "personal"`
  and weaker crypto, THEN the loaded `SecurityConfig` is still federal with FIPS + vault_transit +
  ecdsa-p256 floors intact (real `load_config` + `model_validator`).
- **AC-5 (REQ-014):** WHEN an unsigned blueprint is applied above `personal`, THEN it is refused before
  any config is written; WHEN signed, THEN it verifies and applies.
- **AC-6 (REQ-030):** WHEN `arc extensions` runs against a booted agent, THEN the reported
  selected/available/signed state matches the agent's real `select_*` + `CapabilityRegistry` inventory
  (not a fixture).
- **AC-7 (REQ-015/023):** WHEN a blueprint is applied and WHEN a relaxation is granted, THEN a
  corresponding audit event is emitted with the required fields.

---

## 7. Success metrics

- Net seam-selection LOC **decreases** (duplicated dispatch/gate deleted); total spec net LOC ≤ ~600.
- Zero behavior change in existing brain/skilladapt/capability tests.
- One-command bootstrap: adopter reaches a running, tier-correct agent via a single `arc init --blueprint`.
- arcagent core NCLOC < 3500; full matrix green (`ruff`, `mypy --strict`, all package tests).

## 8. Out of scope / future

- MCP-served extensions and remote blueprint registries (future; the loader is already
  transport-extensible per SPEC-045).
- A blueprint marketplace / discovery service.
- GUI/arcui surface for blueprints (CLI-first this spec).
