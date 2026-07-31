# SPEC-047 — Extensibility + Blueprints Framework

**Feature:** Make **EXTENSIBLE** a first-class product property of Arc. Generalize the two delivered
select-one extension seams (Brain / SPEC-041, SkillAdapter / SPEC-044) into **one** consistent
`ExtensionPoint` + `select_extension` mechanism that new extension points *declare* — **deleting** the
duplicated choice-dispatch / BYO-signing-gate / dotted-path-importer in `brain/select.py` +
`skilladapt/select.py`. Add **signed, versioned preset-config blueprints** (`arc init --blueprint`,
`arc blueprint apply`) for one-command bootstrap, with tier floors non-relaxable through a blueprint.
Give **config-relaxable tiers** one declared surface (`RelaxableKnob` table + shared
`resolve_tier_floor` + audit-on-relaxation) that the existing SPEC-037/043 enforcement points delegate
to. Surface everything via arccli (`arc extensions`, `arc blueprint`).

**Status:** COMPLETE (2026-07-08) — all 8 phases implemented, orchestrator-verified GREEN. arcagent
0.15.0 / arccli 0.6.0. See "Implementation outcome" below.
**Branch:** `feat/SPEC-047-extensibility` (off `develop` @ 213e4df — includes SPEC-041 + SPEC-044).
**Type:** Framework generalization (dedup + WIRE) + preset-config system + CLI surface.
**Phase:** Phase 3 — *end-goal emphasis* (`ROADMAP-PROGRAM.md`).
**Depends on / references:** SPEC-041 (Brain seam + BYO gate), SPEC-044 (SkillAdapter seam), SPEC-021
(capability loader + decorators), SPEC-033 (`.arcsig` sign/verify), SPEC-037 (federal crypto-floor
`model_validator`), SPEC-043 (tier ladder + breaker floors), SPEC-053 (operator-key audit authority),
ADR-019 (tier = stringency, not gate).

---

## Why now

Both extension-point precedents are in-tree and structurally identical: `brain/select.py` and
`skilladapt/select.py` each re-implement the same choice dispatch, the same fail-closed BYO allowlist
gate, and the same dotted-path importer. That's the pattern-of-three trigger — a third extension point
would copy it a third time. The roadmap's explicit next ask after SPEC-044 is SPEC-047: "generalize
them into the first-class extension-point framework + preset-config blueprints + config-relaxable
tiers." This is how Arc is *experienced* by adopters: install arcagent, pick a blueprint, add
capability packages.

## What changes (four moves)

1. **Generalize the select-one seam.** New `arcagent/extension/` (sibling of `core/`, like `brain/`
   and `skilladapt/`): `ExtensionPoint` descriptor + one `select_extension()` carrying the shared
   dispatch + refuse-before-import BYO gate. `brain/select.py` + `skilladapt/select.py` become thin
   instances; their duplicated logic is **deleted** (no-legacy, behavior-preserving, arch tests
   unchanged). Builtin builders (`_try_arcmemory`, `_try_arcskill`) stay.
2. **Declare the four families.** brain/retrieval + skills = select-one instances; tools + hook-builds
   = scan-many views over the **existing** SPEC-021 `CapabilityLoader`/`CapabilityRegistry` (no new
   loader). `arc extensions` inspects all four (selected / available / signed).
3. **Blueprints.** `arcagent/blueprints/` — signed, versioned TOML presets (packaged official +
   `~/.arc/blueprints/` user, `.arcsig` via arctrust). `arc init --blueprint` / `arc blueprint apply`
   verify-before-use **pinned to the deployment operator's key** (above personal an unsigned, tampered,
   or wrong-key preset is refused fail-closed, and an unresolvable operator key denies — an unpinned
   floor is no floor), merge under user config, and floor the tier by stringency-max so a blueprint can
   never weaken federal. Audited on apply (a `--dry-run` writes nothing and audits nothing).
4. **Config-relaxable tiers.** `arcagent/tiers.py` — one `RelaxableKnob` table + `resolve_tier_floor`
   the existing `SecurityConfig` validators delegate to (dedup); federal floors non-relaxable; audit on
   granted relaxation.

## Concern boundary (load-bearing)

- **`arcagent/extension/`** — the select-one engine + family registry + inspection. Imports Protocol
  types only; never statically imports arcmemory/arcskill/any BYO module.
- **`arcagent/brain/`, `arcagent/skilladapt/`** — public seams; now thin `ExtensionPoint` instances +
  builtin builders. Public `select_*` signatures unchanged (call sites in `modules/*/  _runtime.py`
  untouched).
- **`arcagent/blueprints/`** — discovery / verify / merge / tier-floor guard. Reuses
  `capabilities/artifact_signing`. Never agent-writable; operator CLI only.
- **`arcagent/tiers.py`** — the relaxation surface; `core/config.py` validators delegate to it.
- **`arccli/commands/`** — `blueprint.py`, `extensions.py`, `init.py --blueprint`, `registry.py` +2.
- **SPEC-021 CapabilityLoader / arctrust** — UNCHANGED (WIRE, not rebuild).

See SDD §2 for the boundary table, §3 for the mechanism, §4 for blueprints, §5 for tier relaxation.

## Design decisions (recommended defaults — Fable, 2026-07-08; confirm in `/deepen`)

| ID | Decision | Rationale / pillar |
|---|---|---|
| **D-1** | Generalize by **parametrized descriptor** (`ExtensionPoint` = null_factory + builtin_modules + builtin_builder + byo_constructor), not inheritance. One `select_extension` holds the shared dispatch + BYO gate. | Simplicity + Modularity — the seams differ only in builtin package + construction kwargs; a frozen descriptor captures exactly that, deleting the duplication without an ABC hierarchy. |
| **D-2** | `brain/`+`skilladapt/` stay as **public seams**; they become instances, keeping `select_brain`/`select_skill_adapter` signatures so no `_runtime.py` call site changes. | Modularity — behavior-preserving; AC-1 provable against unchanged suites. |
| **D-3** | **Two shapes, four families.** tools + hook-builds are the SAME SPEC-021 loader keyed by decorator kind (`tool` vs `hook`/`background_task`/`capability`); SPEC-047 adds only an inventory read. | Simplicity — no new plugin engine; WIRE-don't-rebuild. |
| **D-4** | Blueprints = **signed TOML** (`[blueprint]` header + config overlay); packaged official + `~/.arc/blueprints/` user; `.arcsig` via existing arctrust `ArtifactSignature`. | Security + Simplicity — reuse SPEC-033 signing; no new crypto or format engine. |
| **D-5** | Merge precedence **packaged < blueprint < user `~/.arc` < per-agent < env**; user config always wins over a blueprint. | Simplicity — a blueprint is a *starting point*, matching how adopters expect presets to behave. |
| **D-6** | Tier floor via **stringency-max**: `effective_tier = max(deployment_tier, blueprint_tier)`; the standard `SecurityConfig` validator then enforces floors. A blueprint can only *raise* stringency. | Security — "personal blueprint cannot weaken federal" true by construction, not by a second check. |
| **D-7** | Above `personal`, an **unsigned/invalid user blueprint is refused before merge** (fail-closed); packaged blueprints trusted by provenance; personal may apply unsigned with audit-warn. | Security — mirrors the capability-loader signature floor; ADR-019 tier stringency. |
| **D-8** | Tier relaxation = **declared table + one shared helper**; existing federal-floor validators delegate to it **iff** core NCLOC stays < 3500, else additive-only (OQ-5). | Security + Simplicity — single enforcement surface without breaching the frozen core budget. |
| **D-9** | Apply + relaxation are **audited** to the operator-signed WORM sink when configured, else telemetry; sink-setup fail-open, never silently dropped when present. | Security — AU-9/AU-10; SPEC-053 operator authority. |
| **D-10** | All new code **outside `arcagent/core/`**; core frozen at 3498/3500. | Simplicity — [[project_loc_budgets_signal_design]]: new homes, not a raised ceiling. |
| **D-11** | Every mechanism has an **E2E-through-real-path** AC (init → load_config → boot → concrete Brain; `arc extensions` over the live registry). | Anti producers-unwired ([[feedback_producers_unwired_pattern]]) — 6 specs in a row caught unwired producers. |
| **D-12** | Honor **D-292** (self-modification tier posture): extensions DISABLED at federal / require approval at enterprise / all enabled at personal — surfaced as `RelaxableKnob`s and reflected by `arc extensions verify`. | Security — consistency with the existing tier posture for self-modification. |

## Open questions (resolved with recommended defaults — none blocking)

- **OQ-1 — Where does the tier-relaxation surface live: `arcagent/tiers.py` (new top-level) vs
  `arcagent/extension/tiers.py`?** *Recommend:* new top-level `arcagent/tiers.py` — tier relaxation is
  broader than extensions (crypto, breakers, budgets, imports) and shouldn't be nested under
  `extension/`. `core/tier.py` (the `Tier` enum) stays the primitive; `tiers.py` is the policy surface.
- **OQ-2 — Should `arc extensions` / blueprint audit go to the operator WORM sink or telemetry by
  default?** *Recommend:* operator WORM sink when the deployment has one configured (enterprise/federal
  via `arc init`), telemetry otherwise (personal). Reuse `_build_worm_sink` from
  `modules/skills/_runtime.py` — no new sink type.
- **OQ-3 — Do packaged official blueprints ship pre-signed, or trusted by provenance (read-only,
  in-package)?** *RESOLVED (deepen prior-art):* provenance-trusted for packaged presets (they ship
  inside the wheel's verified boundary) — but this holds ONLY for blueprints that never leave the
  package tree. Any blueprint entering the override chain from OUTSIDE the wheel (`~/.arc/blueprints/`
  user presets, org/third-party packs) requires an `.arcsig`, verified fail-closed above personal,
  pinned by content hash not a mutable version alias (Sigstore policy-controller TOCTOU lesson). Mirrors
  Helm/Ansible: verification is a hard gate, not advisory.
- **OQ-4 — Which knobs are in the initial `RELAXABLE_KNOBS` table?** *RESOLVED (deepen sweep — see SDD
  §5 pinned table).* Five confirmed floors live in `SecurityConfig` today: `require_fips`, `custody`,
  `signing_algorithm` (all `exact`, forced at federal), `runaway_max_repeat` (floor 8), `error_cascade_max`
  (floor 5) (both `smaller`, `None`=disabled rejected). Start the table with these; add reference entries
  for capability-import policy + budgets + skilladapt change-bound (they keep their existing enforcers) per
  the repo-wide sweep. `resolve_tier_floor` must treat a `None` requested value as "disabled = weakest" for
  `smaller` knobs (not a numeric compare).
- **OQ-5 — Refactor the two `SecurityConfig` federal-floor helpers to delegate to `resolve_tier_floor`
  (dedup, touches core) or keep additive-only?** *RESOLVED (deepen, measured): DELEGATE.* Read-derived
  estimate: the two helpers (`config.py:490-529`, ~39 physical lines) shrink to ~12-16 lines via a
  `RELAXABLE_KNOBS` loop → **net −20 to −25 core NCLOC** (core 3498 → ~3474). **No architecture test blocks
  the `core/config.py → arcagent.tiers` import** (`test_layering.py` only governs package boundaries; no
  top-level allowlist test — T0.3 resolves to "no allowlist edit needed"). The additive-only fallback is
  unnecessary. Confirm with a fresh NCLOC count in T3.3 before landing (figure is read-derived, not run).
- **OQ-6 — Should `arc blueprint apply` support a `--dry-run` that prints the merged config without
  writing?** *Recommend:* yes, cheap and safe (read-only), useful for federal change-control review.
  Fold into T5.1 if LOC budget allows; else defer.
- **OQ-7 — Blueprint provenance recorded in the written config (e.g. a `[blueprint] applied_from`
  stamp) for later `arc extensions` display?** *Recommend:* record it as a telemetry/audit field only,
  not a config schema field (avoids a `SecurityConfig`/`ArcAgentConfig` schema change and keeps the
  written config a plain overlay). Revisit if adopters want `arc extensions` to show "bootstrapped from
  blueprint X".

### New OQs surfaced by /deepen (genuinely need Josh — not resolvable by default)

- **OQ-8 — `arc extensions` command name collides with existing `arc ext` + `arc agent extensions`
  (CONFLICT #2).** Three commands cluster around "extensions": `ext` (capability-file scaffolder,
  `commands/ext.py`), `agent extensions` (capability-file lister, explicitly a "backwards-compat alias"),
  and the proposed top-level `extensions` (4-family inspector). *Recommend:* fold the inspector into the
  existing `ext` command (`arc ext inspect` / `arc ext families`) — it already owns the formatting helpers
  the SDD reuses — or pick a distinct name (`arc points`). **Josh call:** which naming?
- **OQ-9 — Unify the init tier vocabulary `open` → `personal`? (CONFLICT #1).** `arc init` exposes
  `open`/`enterprise`/`federal`; core `Tier` is `personal`/`enterprise`/`federal`. Minimum-viable SPEC-047
  fix = extract the existing inline `open→personal` mapping into one helper + fix the SDD example (no
  rename). *Cleaner but wider:* rename init's `"open"` → `"personal"` end-to-end (touches arcllm-preset
  keys, outside SPEC-047 scope). **Josh call:** minimal-normalize or full rename?
- **OQ-10 — Which file does each blueprint verb write, and what is the resulting runtime precedence?**
  The real `load_config` is 3 layers (`~/.arc` < per-agent < env) with **no blueprint layer**; blueprint
  precedence is resolved at WRITE time. `arc init --blueprint` naturally targets `~/.arc/arcagent.toml`;
  `arc blueprint apply --agent DIR` targets the per-agent file. REQ-012's "user config always wins over
  blueprint" only holds if the write target and merge order are pinned. *Recommend:* init writes `~/.arc`
  (blueprint merged UNDER init flags); apply writes per-agent (blueprint merged UNDER existing per-agent
  keys). **Josh confirm** the target-per-verb + that REQ-012 is re-stated as a write-time ordering.
- **OQ-11 — Does SPEC-047 enforce D-292 "extensions DISABLED at federal", or is that out of scope?** D-292
  governs agent self-modification; the operator BYO select-one gate today *allows* allowlisted extensions
  at federal and does not implement D-292's blanket disablement. *Recommend:* scope SPEC-047 to reflect the
  real BYO gate in `arc extensions verify` (allowlisted-OK at federal) and treat D-292 self-modification
  disablement as a separate arcskill/dynamic-tool concern. **Josh confirm** the scope boundary.

**Scope note (deepen — resolved, not an OQ): SPEC-044's "narrower failure-only insight channel" is OUT
OF SCOPE.** SPEC-044 README (MED-4) flagged a dedicated recurring-failure Brain retrieve-filter as a
possible "arcmemory / SPEC-047 follow-on." SPEC-047 is framework generalization + blueprints + tiers and
does **not** touch arcmemory's retrieval/insight channel — this is deferred as a separate **arcmemory**
follow-on, not part of SPEC-047. Likewise SPEC-044's Hermes H-D (Darwinian code-evolver, "defer to
post-SPEC-047") is self-adaptation, not the select-one seam — correctly absent here.

## Josh's calls (2026-07-09 — LOCKED)

- **OQ-10 blueprint mechanism/precedence:** blueprints are **materialized to disk at setup/apply time** (written into the concrete config the runtime flat-loads — NOT a runtime merge layer; per SDD DC-5/DC-8b). **User config edits always win over a blueprint** (a blueprint is a starting point, not an override); precedence = packaged-defaults < blueprint < user/per-agent hand-edits. `arc blueprint apply` deep-merges, never destructive-template-overwrites (unlike `arc agent build`), and preserves identity.
- **OQ-9 tier vocab:** rename to **`personal` everywhere** — fix the `arc init` `open` inconsistency (incl. the arcllm.toml leak) so all CLI + all three generated files use the security vocab `personal/enterprise/federal`. No `open` remains.
- **OQ-8 extensions command:** do NOT add a colliding top-level `arc extensions`; **extend the existing** `arc ext` / `arc agent extensions` surface (WIRE-don't-rebuild) for the selected/available/signed inspection. Implementer picks the exact verb reconciled with what exists.
- **OQ-11 federal extensibility posture:** federal = **operator MAY install signed + allowlisted extensions at provision/setup time; the agent MAY NOT self-add extensions at runtime** (agent self-modification off, operator provisioning on). Matches SPEC-044's federal treatment (BYO allowlisted OK; agent self-mod gated). This refines D-292 "extensions disabled at federal" to mean *agent-initiated* disabled, *operator-provisioned-signed* allowed.

## Implementation outcome (2026-07-08 — COMPLETE)

Phases 0–3 (dedup select-one seam, family registry + inspect, `tiers.py` + config delegation) landed
in commits `f841a8c` / `2bbca6f`. Phases 4–7:

- **Blueprints** (`arcagent/blueprints/`): `resolve_blueprint` (verify-before-use PINNED to the operator
  key, fail-closed above personal — unsigned/tampered/wrong-key refused, unresolvable operator key denies),
  `apply_blueprint` (deep-merge UNDER user values, stringency-max tier floor), `dumps_toml`,
  `list_blueprints`, 3 provenance-trusted packaged presets. **Verified shipping in the built wheel.**
- **arccli**: `arc blueprint list/show/apply/verify/sign` (+ reusable `apply_to_disk`/`audit_apply`);
  inspection folded into `arc ext inspect` / `arc ext verify`; `arc init --blueprint`; full `open→personal`
  rename.
- **`tiers.audit_tier_relaxations`**: the real production producer for `tier.relaxation_granted` (the
  Pydantic validator can't do I/O — the blueprint-apply path drives it).

**AC evidence (all through real production paths):**
- **AC-3** — `tests/integration/test_blueprint_boot_e2e.py`: materialized blueprint → real
  `__main__._load_config` FLAT read (NOT `load_config`, per DC-8b) → real `ArcAgent.startup` → concrete
  `ArcMemoryBrain` active.
- **AC-4** — federal floor holds through the real `SecurityConfig.model_validator` after a personal
  blueprint on a federal deployment (stringency-max forces FIPS/vault_transit/ecdsa).
- **AC-5** — unsigned/tampered/**wrong-key** user blueprint above personal refused before any merge/write;
  the pin is against the deployment operator key (an unresolvable operator key denies fail-closed).
- **AC-6** — `inspect_extensions` over the booted agent's LIVE `CapabilityRegistry` matches the real
  selected brain + surfaces builtin tools.
- **AC-7** — the real `apply_to_disk` path emits both `tier.relaxation_granted` and `blueprint.applied`.

**Josh-call reconciliations & deviations (logged):**
- **OQ-8** — inspection folded into the existing `arc ext` (`inspect`/`verify`); NO colliding top-level
  `arc extensions`, and only a `blueprint` `CommandDef` was registered (not `extensions`).
- **OQ-9** — `open` fully removed (no alias); all 3 generated files use `personal/enterprise/federal`,
  fixing the arcllm.toml leak. init's `arcagent.toml` is now dict-assembled (`dumps_toml`) to enable the
  `--blueprint` deep-merge.
- **OQ-10** — blueprints materialize to disk; `arc blueprint apply --agent DIR` writes the per-agent file
  the runtime flat-reads (the AC-3 path); `arc init --blueprint` writes the layered `~/.arc` base.
- **SDD §4.1 example was WRONG** — `brain`/`adapter` nest under `[modules.<m>.config]` (the real
  `mod_entry.config` shape), not directly under `[modules.<m>]` (which `ModuleEntry` drops as extra → would
  select NOTHING). Packaged presets use the correct shape; caught as a producers-unwired trap.
- **DC-4** — `arc blueprint sign` works for in-process operator/author keys; a vault_transit federal key
  has no in-process seed and is guarded with a clear error (known limitation, REQ-017 is a Could).

**Adversarial-review fixes (post-implementation, 2026-07-08):**
- **HIGH-1 (merge-blocker) — blueprint signature gate was TOFU-only, pinned nothing.** `resolve_blueprint`
  called `verify_file` with no `trusted_public_key`, so any self-consistent signature was accepted (an
  attacker self-signs a malicious preset with a random keypair). Fixed: above personal the sidecar is
  now pinned to the deployment operator's key (resolved read-only via `operator_public_key(arc_dir)`,
  the same key `arc blueprint sign` signs with); an unsigned/tampered/wrong-key preset is refused, and an
  unresolvable operator key denies fail-closed (mirrors `capability_loader`'s no-pinned-key deny). The
  same pin was threaded into `arc ext inspect`/`verify` `_signed_status` — pinned to the **agent DID key**
  (the authority that signs agent-authored capabilities), so a wrong-key self-signed capability reads
  "unsigned" instead of a false "signed". Driven tests through the real apply + inspect paths.
- **MED-2 — `--dry-run` emitted a real `blueprint.applied` WORM record.** `audit_apply` ran before the
  `if not dry_run` write guard, writing false AU-9/10 provenance for a run that applied nothing. Fixed:
  the audit now lives inside the same guard — a dry run writes no file and emits no record.
- **LOW-3 — operator-key/witness custody paths were not in the overlay denylist.** A blueprint could
  redirect `security.operator_key_dir` / `operator_vault_path` / `notary_keystore` / `witness_medium_path`
  (co-locating the witness with the operator key makes rollback detection illusory). Added all four to
  `_DENIED_OVERLAY_PATHS` so they are stripped before merge.

**Gates:** core NCLOC 3463/3500 · new-module coverage 92.9% (tiers 96%, extension 93–100%, blueprints 88%)
· ruff + mypy --strict clean (arcagent + arccli) · full matrix arcagent 2866 / arccli 365 / arcgateway 585.

## Producers-unwired defense (standing requirement)

Per [[feedback_producers_unwired_pattern]], every mechanism is driven through the real production path,
never a rigged fixture:
- BYO refusal proven through `modules/*/  _runtime.configure`, not a direct `select_extension` call,
  with an import-side-effect sentinel (AC-2).
- Blueprint boot proven `arc init --blueprint X` **writes a materialized config to disk** whose **flat
  load** (the real `arcagent/__main__.py` runtime path — NOT `load_config`, which the runtime bypasses;
  see SDD DC-5) → real `ArcAgent.startup` → concrete selected Brain (AC-3). Blueprints are rendered-then-
  written at init/apply time, not applied as a runtime merge layer.
- Federal floor proven through the real `SecurityConfig.model_validator` after a blueprint apply (AC-4).
- `arc extensions` proven against the live config + real `CapabilityRegistry` (AC-6).

## Net-LOC estimate

| | LOC |
|---|---|
| New: `extension/` (point + select + families + inspect) | ~+230 |
| New: `blueprints/loader.py` + 3 packaged TOMLs (data) | ~+150 |
| New: `arcagent/tiers.py` | ~+70 |
| New: `arccli` blueprint.py + extensions.py + init `--blueprint` + registry | ~+230 |
| **Deleted:** duplicated dispatch/gate/`_load_custom` in the two `select.py` | ~−120 |
| **Net** | **~+560** (under the ~600 ceiling; **seam-selection LOC decreases**) |

All new arcagent code lands **outside `core/`**; core stays < 3500 NCLOC (frozen at 3498).

## Baseline (captured at implement start, T0.1 — 2026-07-08)

- arcagent core NCLOC: **3498 / 3500** (< 3500 required)
- `brain/` test count: **5** · `skilladapt/` test count: **10** (both pass unchanged post-refactor, AC-1)
- AC-1 enumerated mechanical edit: `tests/unit/brain/test_select.py` `_patch_import` repoints
  `select.importlib` → `arcagent.extension.select.importlib` (BYO import moved to shared mechanism).
  skilladapt tests use real imports → zero edits.

## Next steps

1. `/deepen SPEC-047` — sweep every `if tier == "federal"` site for OQ-4; confirm the `ExtensionPoint`
   descriptor covers both builtin construction shapes without leaking builder kwargs; verify no arch
   test hard-codes the two-family assumption.
2. Present OQs (esp. OQ-5 core-budget call) to Josh.
3. `/implement SPEC-047` — Phase 0 baseline first, then dedup-before-new-surface.
