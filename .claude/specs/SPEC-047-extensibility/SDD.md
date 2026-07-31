# SPEC-047 — Extensibility + Blueprints Framework · SDD

Traceability: every component maps to PRD REQ-NNN. Pillars ranked Simplicity → Modularity → Security
→ Scalability (principled-coder).

---

## 1. Design overview

Three additive subsystems, all **outside arcagent core**, plus a behavior-preserving dedup of the two
existing seams:

```
                       ┌─────────────────────────────────────────────┐
                       │  arcagent/extension/   (NEW, sibling of core)│
   select-one seams    │  point.py     ExtensionPoint descriptor      │
   ┌───────────────┐   │  select.py    select_extension()  ◄──────────┼── the DEDUP target:
   │ brain/select  │──►│               (dispatch + BYO gate + import) │   one copy of the
   │ skilladapt/   │──►│  families.py  the 4-family registry          │   shared choice/gate
   │   select      │   │  inspect.py   inspect_extensions() for CLI   │
   └───────────────┘   └─────────────────────────────────────────────┘
                       ┌─────────────────────────────────────────────┐
   scan-many families  │  SPEC-021 CapabilityLoader / CapabilityRegistry (UNCHANGED)
   (tools, hook-builds)│  → families.py reads its inventory for `arc extensions`
                       └─────────────────────────────────────────────┘
                       ┌─────────────────────────────────────────────┐
   blueprints          │  arcagent/blueprints/   (NEW)                │
                       │  loader.py    discover / verify / merge      │
                       │  *.toml       packaged official presets      │
                       └─────────────────────────────────────────────┘
                       ┌─────────────────────────────────────────────┐
   tier relaxation     │  arcagent/tiers.py  (NEW)                    │
                       │  RelaxableKnob table + resolve_tier_floor()  │
                       │  + audit-on-relaxation; core/config.py       │
                       │  validators delegate to it (dedup)           │
                       └─────────────────────────────────────────────┘
                       ┌─────────────────────────────────────────────┐
   arccli surface      │  commands/blueprint.py   list/show/apply/    │
                       │                          verify/sign         │
                       │  commands/extensions.py  list/verify         │
                       │  commands/init.py        + --blueprint flag  │
                       │  commands/registry.py    + 2 CommandDefs     │
                       └─────────────────────────────────────────────┘
```

**Load-bearing WIRE-don't-rebuild stances:**
1. The scan-many families are the **existing** SPEC-021 loader — SPEC-047 adds no loader code, only an
   inventory read for inspection.
2. Blueprint signing is the **existing** arctrust `ArtifactSignature` + SPEC-033 `.arcsig` sidecar —
   no new crypto.
3. Tier-floor enforcement stays at the **existing** points (`SecurityConfig` validators, dynamic
   loader, dispatch); SPEC-047 extracts the repeated idiom into one shared helper they call.

---

## 2. Module boundaries (explicit contracts)

| Module | Owns | Imports | MUST NOT |
|---|---|---|---|
| `arcagent/extension/` | The select-one mechanism + family registry + inspection | stdlib, `arcagent.brain`, `arcagent.skilladapt` (Protocol types only), `arcagent.capabilities` (registry read) | import arcmemory / arcskill / any BYO module statically; contain family-specific build logic |
| `arcagent/brain/select.py` | The Brain `ExtensionPoint` instance + `_try_arcmemory` builder | `arcagent.extension`, lazy `arcmemory` | re-implement dispatch / BYO gate |
| `arcagent/skilladapt/select.py` | The SkillAdapter `ExtensionPoint` instance + `_try_arcskill` builder | `arcagent.extension`, lazy `arcskill` | re-implement dispatch / BYO gate |
| `arcagent/blueprints/` | Blueprint discovery, signature verify, config merge, tier-floor guard | `arcagent.tiers`, `arcagent.capabilities.artifact_signing` (reuse), `tomllib` | write agent state; execute agent code; weaken a tier floor |
| `arcagent/tiers.py` | `RelaxableKnob` table + `resolve_tier_floor` + audit-on-relaxation | `arcagent.core.tier` (Tier enum only) | own crypto or config parsing |
| `arccli/commands/blueprint.py`, `extensions.py` | CLI verbs (operator surface) | `arcagent.blueprints`, `arcagent.extension` (lazy, in-handler) | be reachable as an agent tool |

The extension mechanism speaks only **structural Protocols + primitives** at its boundary, exactly as
the Brain/SkillAdapter Protocols do — arcagent never names an implementation type.

---

## 3. The generalized select-one mechanism

### 3.1 `ExtensionPoint` descriptor (`extension/point.py`)

A frozen dataclass parametrizing everything that differs between the two seams; everything that is the
same lives in `select_extension`.

```python
@dataclass(frozen=True)
class ExtensionPoint:
    name: str                                    # "brain" | "skills" | ...
    null_factory: Callable[[], Any]              # NullBrain / NullSkillAdapter
    builtin_modules: Mapping[str, str]           # {"arcmemory": "arcmemory", "auto": "arcmemory"}
    builtin_builder: Callable[[Any, dict[str, Any]], Any | None]
        # (imported_module, context) -> instance | None (None = degrade to Null)
    byo_constructor: Callable[[type, dict[str, Any]], Any]
        # (resolved_cls, context) -> instance  (Brain: cls(ws, did); adapter: cls(ws))
    kind: Literal["select_one", "scan_many"] = "select_one"
```

### 3.2 `select_extension` (`extension/select.py`) — the one copy of the shared logic

```python
def select_extension(
    point: ExtensionPoint, setting: str, *, tier: str,
    allowlist: tuple[str, ...], context: dict[str, Any], logger: Logger,
) -> Any:
    choice = (setting or "none").strip()
    if choice in ("none", "", "null"):
        return point.null_factory()
    if choice in point.builtin_modules:
        module_name = point.builtin_modules[choice]
        instance = _try_builtin(point, module_name, context)       # lazy import + build
        if instance is not None:
            return instance
        if choice != "auto":
            logger.warning("%s=%r but %s unavailable; degrading to Null",
                           point.name, choice, module_name)
        return point.null_factory()
    return _load_byo(point, choice, tier=tier, allowlist=allowlist, context=context)
```

`_load_byo` carries the **exact** SPEC-041/044 gate — refuse-before-import above personal, then
`module.rpartition`, `importlib.import_module`, `byo_constructor`. This function is the single home of
the logic currently duplicated in both `select.py` files.

### 3.3 The two seams become instances

`brain/select.py` (illustrative):

```python
_BRAIN_POINT = ExtensionPoint(
    name="brain",
    null_factory=NullBrain,
    builtin_modules={"arcmemory": "arcmemory", "auto": "arcmemory"},
    builtin_builder=_build_arcmemory,          # the current _try_arcmemory body, unchanged
    byo_constructor=lambda cls, ctx: cls(ctx["workspace"], ctx["agent_did"]),
)

def select_brain(setting, *, workspace, agent_did, tier="personal", brain_allowlist=(), **kw) -> Brain:
    ctx = {"workspace": workspace, "agent_did": agent_did, "tier": tier, **kw}
    return select_extension(_BRAIN_POINT, setting, tier=tier,
                            allowlist=tuple(brain_allowlist), context=ctx, logger=_logger)
```

`skilladapt/select.py` mirrors it with `_build_arcskill` and `byo_constructor=lambda cls, ctx:
cls(ctx["workspace"])`. **Deleted** from each file: the `choice`-dispatch block, the `_load_custom`
importer, and the inline BYO-gate `if tier != "personal" and class_path not in allowlist: raise`.
Public signatures of `select_brain` / `select_skill_adapter` are unchanged (REQ-004) so
`modules/memory/_runtime.py` and `modules/skills/_runtime.py` call sites need no edit.

### 3.4 Family registry (`extension/families.py`)

Declares the four families for inspection (REQ-006/030). Select-one families carry their
`ExtensionPoint` + the config dotpath that selects them (`modules.memory.brain`,
`modules.skills.adapter`). Scan-many families carry a reader over the live `CapabilityRegistry`,
filtered by decorator kind: `tools` = `{"tool"}`; `hook-builds` = `{"hook", "background_task",
"capability"}`.

### 3.5 Inspection (`extension/inspect.py`)

`inspect_extensions(config, registry=None) -> list[ExtensionStatus]`:
- select-one: read the configured setting; probe availability (is the builtin importable? is a BYO
  class-path allowlisted?); report signed-status (BYO signature check where applicable).
- scan-many: enumerate the registry's tools/hooks by kind; report each capability's source root and
  `.arcsig` verification result.

Pure read; no side effects; safe to run against a booted or a config-only context.

### Research Insights (/deepen — DC seam verification vs. real code)

**DC-1 — the "byte-identical" claim is OVERSTATED; the design still holds.** `brain/select.py`
and `skilladapt/select.py` are structurally *parallel*, not byte-for-byte identical (PRD §1 / this
SDD's opening should be softened to "structurally parallel"). What is genuinely duplicated — and worth
extracting — is only the BYO gate + dotted-path importer (`_load_custom`): both files carry the
identical `if tier != "personal" and class_path not in allowlist: raise` fail-closed gate, then
`class_path.replace(":", ".").rpartition(".")` → `importlib.import_module` → `getattr`. What *differs*
between the two seams:
- **Dispatch:** brain collapses `("arcmemory", "auto")` into one branch and warns only when the choice
  was the explicit `"arcmemory"` (not `"auto"`); skills has a single `"arcskill"` branch and **no
  `auto`**. The descriptor captures this via `builtin_modules` (`{"arcmemory": "arcmemory", "auto":
  "arcmemory"}` vs `{"arcskill": "arcskill.improver"}`) + the `choice != "auto"` warn guard — confirmed
  sufficient.
- **Builtin import target differs:** brain imports the top package `arcmemory` (then reads
  `.MemoryConfig`/`.ArcMemoryBrain`); skills imports the **submodule** `arcskill.improver`. So the
  descriptor's `builtin_modules` value must be the *actual import string* (`"arcskill.improver"`, not
  `"arcskill"`).
- **Builder kwargs differ substantially:** `_try_arcmemory` takes `(workspace, agent_did, tier,
  audit_sink, *, embed_backend, embed_model, distill_provider, distill_model)`; `_try_arcskill` takes
  `(workspace, *, config, tier, llm, signer, approval_provider, eval_runner, audit_sink, agent_did,
  skill_path)`. These become entries in the `context` dict — fine.
- **BYO construction differs:** brain `cls(workspace, agent_did)`; skills `cls(workspace)` (single arg).
  Captured by `byo_constructor` — confirmed.

**CORRECTION to §3.3:** the SDD says the builtin builder is "the current `_try_arcmemory` body,
unchanged." That is **inaccurate**. Under the generalized design, `_try_builtin` performs the lazy
`importlib.import_module(module_name)` and passes the imported module to
`builtin_builder(imported_module, context)`. The current `_try_arcmemory`/`_try_arcskill` **do their
own import internally** and take explicit kwargs — so each builder MUST be adapted to the
`(imported_module, context) -> instance | None` signature with the import lifted out and kwargs pulled
from `context`. The *logic* is preserved; the *signature* changes. Re-word §3.3 to say "adapted to the
`(module, context)` builder signature," not "unchanged."

**DC-2 — callers CONFIRMED, with an AC-1 caveat.** Call sites are `modules/memory/_runtime.py:66`
(`select_brain(cfg.brain, workspace=ws, agent_did=agent_did, tier=cfg.tier, embed_backend=…, …,
brain_allowlist=tuple(cfg.brain_allowlist))`) and `modules/skills/_runtime.py:108`
(`select_skill_adapter(cfg.adapter, workspace=ws, config=cfg.improver, tier=cfg.tier, llm=…, signer=…,
approval_provider=…, audit_sink=…, agent_did=…, skill_path=_skill_path,
adapter_allowlist=tuple(cfg.adapter_allowlist))`). Both are keyword-only. **Caveat:** §3.3's
illustrative wrapper `def select_brain(setting, *, workspace, agent_did, tier="personal",
brain_allowlist=(), **kw)` collapses the explicit kwargs into `**kw`. AC-1 requires "identical public
signatures" and `mypy --strict` benefits from explicit typed params. **Keep the explicit keyword
parameters** in each thin wrapper (workspace, agent_did, tier, embed_backend, …) and build `context`
explicitly; do not collapse to `**kw`. (Note: neither caller passes `audit_sink` to `select_brain` nor
`eval_runner` to `select_skill_adapter` — those defaulted params can stay or be dropped, but changing
them is a signature change and must be enumerated under AC-1 if touched.)

**DC-3 — decorator kinds CONFIRMED, with a coverage gap.** Registry kinds are `Kind =
Literal["tool", "skill", "hook", "background_task", "capability"]` (`capability_registry.py:50`);
loader dispatch is `isinstance(meta, ToolMetadata | HookMetadata | BackgroundTaskMetadata |
CapabilityClassMetadata)` (`capability_loader.py:312-342`). The SDD's family mapping — `tools = {"tool"}`,
`hook-builds = {"hook", "background_task", "capability"}` — is correct. **GAP:** the registry's fifth
kind, `"skill"` (loaded SKILL.md *capability* folders in `registry._skills`), is covered by **neither**
scan-many family, so `arc extensions` would not surface loaded skill-capabilities. This also collides
terminologically with the "skills" *select-one* family (the improver **adapter**). Recommend §3.4
explicitly state that the "skills" family = the SkillAdapter select-one seam, and either add a fifth
scan-many view for the `"skill"` kind or note its deliberate exclusion. `inspect.py` can read the
registry via the private `_tools`/`_skills`/`_hooks`/`_tasks`/`_capabilities` dicts (entries carry
`source_path` + `scan_root`), the same pattern `skills/_runtime.py` already uses — or a small public
snapshot method would be cleaner.

---

## 4. Blueprints

### 4.1 Format (REQ-010)

```toml
[blueprint]
name = "federal-analyst"
version = "1.0.0"
tier = "federal"
description = "SCIF-ready analyst: memory on, signed capabilities, FIPS floors."

# ---- config overlay (same shape as arcagent.toml) ----
[security]
tier = "federal"

[modules.memory]
enabled = true
brain = "arcmemory"

[modules.skills]
enabled = true
adapter = "arcskill"
```

Packaged official blueprints ship at `arcagent/blueprints/*.toml`:
`personal-assistant.toml`, `enterprise-ops.toml`, `federal-analyst.toml`. User/global blueprints live
at `~/.arc/blueprints/*.toml`, each with a `<name>.toml.arcsig` sidecar.

### 4.2 Discovery, verify, merge (`blueprints/loader.py`)

```python
def resolve_blueprint(name: str, *, tier: str) -> ResolvedBlueprint          # find + verify-before-use
def apply_blueprint(overlay: dict, base: dict, *, deployment_tier: str) -> dict  # merge + tier-floor guard
```

- **Verify-before-use (REQ-014), PINNING ENFORCED:** load `.arcsig`, `verify_artifact(content, manifest,
  trusted_public_key=<deployment operator pubkey>)`. Above `personal` the pin is **mandatory**, not
  best-effort — an unpinned signature gate accepts any self-consistent signature (an attacker self-signs
  a malicious preset with a random keypair), so an unpinned floor is no floor. The operator pubkey is the
  key `arc blueprint sign` signs with, resolved read-only via `operator_public_key(arc_dir)`
  (`arccli/commands/operator.py`). When it **cannot be resolved** above personal, resolution DENIES
  fail-closed (mirrors `capability_loader`'s require-signature-with-no-pinned-key deny). A user blueprint
  that is unsigned, tampered, or **signed by the wrong key** is refused before merge. Packaged blueprints
  are trusted by provenance (shipped in-package, read-only). Personal may apply an unsigned user
  blueprint with an audit-warn (unpinned verify permitted at personal only).
- **Merge precedence (REQ-012):** the blueprint overlay is inserted **just above packaged defaults and
  below the user `~/.arc` config** in the existing `load_config` layering — implemented by writing the
  merged result at `arc init` time, or by deep-merging `blueprint < existing-user-config` at
  `arc blueprint apply` time. A user's explicit key always wins.
- **Tier-floor guard (REQ-013):** before merge, `effective_tier =
  max_stringency(deployment_tier, blueprint.tier)` where federal > enterprise > personal; the merged
  `[security].tier` is set to `effective_tier` and the blueprint may never lower it. The standard
  `SecurityConfig` `model_validator` then runs on load and enforces every floor for `effective_tier`.
  This is the mechanism that makes "a personal blueprint cannot weaken federal" true by construction.

### 4.3 Audit on apply (REQ-015)

`apply_blueprint` (CLI path) emits `blueprint.applied` with `{name, version, sha256, signer_did,
effective_tier}` to the operator-signed WORM sink when the deployment has one configured (reuse the
`_build_worm_sink` pattern from `modules/skills/_runtime.py`), else to telemetry. Sink-setup failure
is fail-open (never blocks apply) but a present sink always receives the record.

### 4.4 Four Pillars on blueprints

- **Identity:** the `.arcsig` carries the signer DID (`ArtifactSignature.signer_did`).
- **Sign/verify:** reuse `arcagent/capabilities/artifact_signing.py` (`write_signature`,
  `verify_file`) — no new signing code; `arc blueprint sign` calls `write_signature`.
- **Authorize:** applying is an operator CLI action (never an agent tool); tier floors non-relaxable.
- **Audit:** REQ-015 event on every apply.

### Research Insights (/deepen — blueprint seams vs. real code)

**DC-4 — signing reuse CONFIRMED.** `capabilities/artifact_signing.py` exposes
`write_signature(artifact, content, *, signer_did, private_key) -> Path` and `verify_file(artifact,
content, *, trusted_public_key=None) -> bool` over arctrust `sign_artifact` / `verify_artifact`;
`ArtifactSignature.signer_did: str` exists (`arctrust/artifact.py:42`); `WormSink(chain,
operator_signer)` is exported and is exactly what `modules/skills/_runtime.py:_build_worm_sink` reuses.
Blueprint sign/verify/audit can all be built on these with **no new crypto**, as claimed.
- **REQ-017 caveat (`arc blueprint sign`):** `write_signature` takes a raw `private_key: bytes`. The
  CLI already has `arccli/commands/operator.py:resolve_operator_signer() -> Signer` and
  `load_operator_key()`, but a **federal `custody = vault_transit` operator key has no in-process
  seed** — there is no raw `private_key` to hand `write_signature`. So `arc blueprint sign` works for
  in-process operator/author keys (personal/enterprise authoring) but a vault-held federal key needs a
  `Signer`-based signing path, not raw bytes. Since REQ-017 is a *Could* and packaged blueprints are
  trusted-by-provenance (unsigned), this is a known limitation to record, not a blocker.

**DC-8 — config precedence is BROKEN AS SPECIFIED; this is the load-bearing correction.** The real
loader `core/config.py:load_config` (line 674) has **three** layers only:
`${ARC_CONFIG_DIR:-~/.arc}/arcagent.toml` (base) → per-agent `path` → `ARCAGENT_` env vars, deep-merged
via `_deep_merge` (line 650; dicts merge, scalars/lists replace). There is **no packaged-defaults layer**
(Pydantic model defaults serve that role implicitly) and **no blueprint layer or insertion hook**. The
SDD §4.2 / D-5 claim that "the blueprint overlay is inserted just above packaged defaults and below the
user `~/.arc` config in the existing `load_config` layering" has **no hook point** and must be dropped.
- **Viable path (already the SDD's alt):** resolve blueprint precedence at **WRITE time** — deep-merge
  `blueprint_overlay < user-supplied-values` and write the concrete result. `arc init --blueprint`
  writes `~/.arc/arcagent.toml` (the user-wide base); `arc blueprint apply --agent DIR` writes the
  per-agent file. Re-state REQ-012 as a **write-time merge ordering**, not a runtime 5-layer chain.
- **Precedence hazard to resolve explicitly:** at runtime the per-agent file **overrides** `~/.arc`.
  If a blueprint is written into `~/.arc/arcagent.toml` but the operator later hand-edits a per-agent
  file, the per-agent file wins (fine). But if a blueprint is instead merged into a *per-agent* file,
  it would then win over `~/.arc` user config — the **opposite** of REQ-012's "user config always wins
  over blueprint." The spec must pin *which file each verb targets* and state the resulting order. AC-4
  (federal floor holds) is unaffected: it depends only on the written `[security].tier` being the
  stringency-max and the real `model_validator` running at load — which holds regardless of layering.
- **Additional at-risk seam (init writer shape):** `arc init` builds TOML by **f-string templating**
  (`init.py:_generate_arcagent_toml`, hand-emitted `[section]` lines), **not** a dict → TOML dump. So
  "deep-merge the blueprint overlay into the generated `arcagent.toml`" (§6.3) has no dict-merge hook —
  it would require either parsing the emitted string back to a dict, merging, and re-dumping, or
  refactoring `init` to assemble a dict and dump it (no `tomli_w` path exists today). T5.3 must own this
  refactor; appending raw blueprint `[section]` blocks risks duplicate-key TOML.
- **DC-8b — the `__main__.py` FLAT-LOADER BYPASS (a named BROKEN seam the design MUST handle).** The
  agent runtime entrypoint `arcagent/__main__.py:48-59` does **not** call `load_config` — it loads a
  single flat file: `raw = tomllib.loads(config_path.read_text()); ArcAgentConfig.model_validate(raw)` —
  **no user layer, no env overrides, no deep-merge.** Layered `load_config` is used only by CLI/gateway
  paths (`arccli/agent_worker.py:97,127`, `commands/team.py`, `commands/agent/create.py`,
  `commands/agent/reload.py`, `arcgateway/bootstrap.py`). **Consequence:** a blueprint-merge hook added
  inside `load_config` would be **DEAD at agent runtime** — the running agent reads its per-agent
  `arcagent.toml` flat. Decisive for the design: blueprints MUST be resolved **at write time into the
  concrete `arcagent.toml`** that `__main__.py` reads flat; a runtime merge layer is not an option. AC-3's
  E2E must boot through the path that actually reads the written file. (Env denylist FYI: `vault__backend`,
  `tools__process`, `tools__preamble`, `identity__key_dir` cannot be env-overridden — `config.py:602-609`.)
- **Blueprint-apply must NOT clone `arc agent build`.** `arc agent build` (interactive) writes a fully
  **hardcoded template** via `config_path.write_text(...)` unconditionally — no merge, no backup, no
  confirm — and resets `[identity] did = ""` (`commands/agent/build.py:216-284`); only `--check` is
  read-only. `arc blueprint apply` must **deep-merge under existing config** and never clone this
  clobber-write (it would wipe identity + user keys). `~/.arc/blueprints/` **does not exist today** (zero
  `blueprint` code hits repo-wide) — entirely new, and per DC-8b must be wired at the **write** boundary.

---

## 5. Config-relaxable tiers (`arcagent/tiers.py`)

### 5.1 Declared surface (REQ-020)

```python
@dataclass(frozen=True)
class RelaxableKnob:
    name: str                      # "custody", "runaway_max_repeat", ...
    federal_floor: Any             # the pinned value; None = "must be set / cannot disable"
    relax_personal: bool           # may personal loosen it?
    relax_enterprise: bool         # may enterprise loosen it?
    stricter_is: Literal["smaller", "larger", "exact"]  # ordering of "stronger"

RELAXABLE_KNOBS: tuple[RelaxableKnob, ...] = (
    RelaxableKnob("custody", "vault_transit", True, True, "exact"),
    RelaxableKnob("signing_algorithm", "ecdsa-p256", True, True, "exact"),
    RelaxableKnob("require_fips", True, True, True, "exact"),
    RelaxableKnob("runaway_max_repeat", 8, True, True, "smaller"),
    RelaxableKnob("error_cascade_max", 5, True, True, "smaller"),
    # capability import policy, budgets, skilladapt change-bound reference existing enforcers
)
```

### 5.2 Shared enforcement helper (REQ-021/022)

```python
def resolve_tier_floor(knob: RelaxableKnob, tier: Tier, requested, *, was_set: bool,
                       audit=None) -> Any:
    """Return the value to use; raise on an explicit weaker-than-floor value at a tier
    that forbids relaxation; audit a granted relaxation."""
```

`SecurityConfig._reject_weaker_federal_override` and `_apply_federal_breaker_floors` are refactored to
loop over the federal-relevant knobs and delegate to `resolve_tier_floor` — the same fail-closed
outcomes, one implementation. This edit touches `core/config.py`; it is a **replacement, net-neutral or
negative** on core NCLOC (inline idiom → shared call). REQ-021 makes the delegation conditional on core
staying < 3500; if measurement shows it would breach, ship the helper additively (new knobs + blueprint
path use it; the two existing validators stay) and record the residual duplication as tracked debt
(OQ-5).

### 5.3 Audit on relaxation (REQ-023)

When `personal`/`enterprise` supplies a value the floor permits loosening and it differs from the
default, `resolve_tier_floor` emits `tier.relaxation_granted {knob, tier, requested, resolved}`. The
blueprint-apply path passes its audit sink through so a blueprint-driven relaxation is recorded too.

### Research Insights (/deepen — tier-floor seams, RELAXABLE_KNOBS, OQ-5 measurement)

**DC-5 — validator names CONFIRMED.** `core/config.py` `SecurityConfig` has the
`@model_validator(mode="after") _enforce_tier_crypto_floor` (line 470) which calls
`_reject_weaker_federal_override` (line 512, exact-match crypto knobs) and
`_apply_federal_breaker_floors` (line 490, cap knobs). Federal forces `require_fips=True`,
`custody="vault_transit"`, `signing_algorithm="ecdsa-p256"` (lines 482-484) and pins breaker floors via
the module constants `_FEDERAL_RUNAWAY_FLOOR = 8`, `_FEDERAL_CASCADE_FLOOR = 5` (lines 331-332). The two
named helpers are exactly the delegation targets the SDD assumes — verified.

**RELAXABLE_KNOBS — pinned table (core/config.py floors, verified directly).** These five are the floors
inside `SecurityConfig` today; the repo-wide sweep (below) adds import-policy / budget / change-bound /
self-modification entries.

| knob | owning config · file:line | federal floor | relax @personal | relax @enterprise | stricter_is |
|---|---|---|---|---|---|
| `require_fips` | `SecurityConfig` · config.py:434 / forced 482 | `True` | yes (default `False`) | yes | exact |
| `custody` | `SecurityConfig` · config.py:416 / forced 483 | `"vault_transit"` | yes (default `in_process`) | enterprise default is `vault_transit` (486-487), relaxable to `in_process` | exact |
| `signing_algorithm` | `SecurityConfig` · config.py:408 / forced 484 | `"ecdsa-p256"` | yes (default `ed25519`) | yes | exact |
| `runaway_max_repeat` | `SecurityConfig` · config.py:368 / floor 497-503 | `8` (`None`=disabled → rejected) | yes (default `None`) | yes | smaller |
| `error_cascade_max` | `SecurityConfig` · config.py:369 / floor 504-510 | `5` (`None`=disabled → rejected) | yes (default `None`) | yes | smaller |

**Repo-wide sweep — additional floored knobs (enforced OUTSIDE the `SecurityConfig` validator).** These
are real tier-relaxable surfaces the `RELAXABLE_KNOBS` table should *reference* even though their
enforcement lives elsewhere (the table can carry them as reference entries pointing at their existing
enforcers, per OQ-4's original intent — do NOT relocate enforcement into `tiers.py`):

| knob | owning config · enforcement site | federal floor | relax @personal | relax @enterprise | stricter_is |
|---|---|---|---|---|---|
| `budget.max_tokens` | `BudgetConfig` config.py:66 · resolved at dispatch `tools/_policy_fill.py:31`, tightened `core/agent_dispatch.py:262` | `500_000` when unset | yes (unbounded) | yes (`2_000_000`) | smaller |
| `budget.max_cost_usd` | `BudgetConfig` config.py:67 · `_policy_fill.py:32` | `10.0` when unset | yes (unbounded) | yes (`50.0`) | smaller |
| `budget.max_requests` | `BudgetConfig` config.py:68 · `_policy_fill.py:33` | `500` when unset | yes (unbounded) | yes (`2_000`) | smaller |
| `allow_all_imports` | `CapabilitiesConfig` config.py:551 · `tools/_dynamic_loader.py:335` (`resolve_workspace_import_policy`) | **IGNORED at federal** (blanket relaxation denied) | moot | honored (:359) | federal ignores |
| `allow_imports` (allowlist) | `CapabilitiesConfig` config.py:558 · `_dynamic_loader.py:357` | honored (the ONLY import relaxation at federal) | moot | honored | additive allowlist |
| approval-required tool set | `resolve_approval_set` `tools/approval_policy.py:33` | ALL capabilities (tools+skills) | opt-in only | all plain tools + opt-ins | larger set = stricter |
| HumanGate auto-approve | `tools/human_gate.py:91` | federal FORBIDS auto-approve entirely | named low-risk compositions | named compositions | federal = none |
| BYO brain class-path | `brain/select.py:_load_custom:138` gate :142 | must be operator-allowlisted | free | allowlist required | allowlist = stricter |
| BYO skills-adapter class-path | `skilladapt/select.py:_load_custom:105` gate :113 | must be operator-allowlisted | free | allowlist required | allowlist = stricter |

**Mechanism note (important for the delegation design):** breakers, budgets, and the approval set are
**NOT enforced in a Pydantic validator** — they follow the "arcagent resolves the ceiling, arcrun
enforces at dispatch" split (`_effective_ceilings`/`resolve_run_budget` → threaded onto the arcrun loop
in `core/agent_dispatch.py:262`, where `_tighter` picks the lower value). And `require_fips` only *arms*
the floor in config; the actual assertion is downstream in `arctrust/fips.py:assert_fips_if_required`.
So `resolve_tier_floor` cleanly dedups the **`SecurityConfig` validator** floors (the 5 crypto/breaker
knobs above); the budget/import/approval knobs keep their existing dispatch/resolver enforcers and
appear in `RELAXABLE_KNOBS` only as **declarative reference rows** for `arc extensions verify` + audit —
not as new enforcement paths. **D-292 / skilladapt change-bound:** no `change_bound` symbol exists; the
only tier gate in skilladapt is the BYO allowlist (`select.py:113`), mirror of the BYO-brain gate —
binary (personal-free / above-personal-allowlist-required), not a numeric floor.

Semantics `resolve_tier_floor` must honor: for `stricter_is="smaller"`, a **`None` requested value means
disabled = weakest** and must be rejected-if-explicitly-set / pinned-if-unset (not a numeric compare);
for `"exact"`, federal forces the floor value regardless and rejects any explicitly-set different value.
The RelaxableKnob field `federal_floor=None` (SDD §5.1 comment "must be set / cannot disable") is a
DIFFERENT use of `None` than a `None` *requested* value — name them apart in the implementation.

**OQ-5 — measured recommendation: DELEGATE (dedup path), core NCLOC goes DOWN ~24 lines.** The two
helpers span config.py:490-529 (~39 physical lines, ~27 non-docstring logic lines). Replacing their
bodies with a loop over the federal-relevant `RELAXABLE_KNOBS` calling `resolve_tier_floor` reduces each
to ~5-7 lines (~12-16 total), a **net −20 to −25 core NCLOC** (plus one `from arcagent.tiers import …`
line). Core is at 3498/3500 → post-edit ~3474, comfortable headroom. **No architecture test blocks the
`core/config.py → arcagent.tiers` import** — `orchestration/test_layering.py` only governs
arcagent↔{arcrun,arcllm,arcstore} boundaries and spawn ownership; there is no top-level-package
allowlist test, so T0.3's "does adding `extension`/`blueprints`/`tiers` need an allowlist edit?" resolves
to **no**. Recommendation: take the delegation (D-8/OQ-5 primary path); the additive-only fallback is
unnecessary given the measured negative delta. Confirm with a fresh NCLOC count in T3.3 (this figure is
read-derived, not run). The Pydantic `model_validator` cannot move out of core (it is bound to
`SecurityConfig`), so only its body shrinks — enforcement *policy* lives in `tiers.py`, the *hook* stays
in core. `tiers.py` must also define the tier stringency ordering (federal > enterprise > personal); the
`Tier` StrEnum has `is_federal`/`is_enterprise`/`is_personal` but **no numeric order** today.

---

## 6. arccli surface

### 6.1 `arc blueprint` (`commands/blueprint.py`)
`list` (packaged + `~/.arc/blueprints`, columns name/version/tier/signed), `show <name>` (resolved
overlay), `apply <name> [--agent DIR] [--dir ~/.arc]` (verify → merge → write → audit),
`verify <name>` (signature validity), `sign <path>` (operator-sign a user blueprint). Mirrors the
`arc skill` / `arc ext` command structure and `_write`/`_print_table` helpers.

### 6.2 `arc extensions` (`commands/extensions.py`)
`list` (default): render `inspect_extensions()` — family, kind, selected, available, signed.
`verify`: report any selection/capability that would be refused at load under the current tier.

### 6.3 `arc init --blueprint <name>` (`commands/init.py`)
Adds a `--blueprint` flag; after tier/provider resolution, resolves + verifies the blueprint and
deep-merges its overlay into the generated `arcagent.toml` (blueprint below the user's flag-driven
choices), setting the stringency-max tier. Emits the apply audit event.

### 6.4 Registry (`commands/registry.py`)
Two new `CommandDef`s (`blueprint`, `extensions`) in `COMMAND_REGISTRY`, category `Tools & Skills`,
lazy handlers per the existing convention.

### Research Insights (/deepen — arccli surface conflicts vs. real code)

**CONFLICT #2 — `arc extensions` collides with two existing commands.** The registry already has a
top-level **`ext`** command (`CommandDef name="ext"`, `commands/ext.py:ext_handler` — a capability-file
*scaffolder*: `list`/`create`/`install`/`validate`, with its own `_write`/`_print_table` helpers), AND
an **`arc agent extensions`** subcommand (`commands/agent/extensions.py`) whose own docstring says
*"`extensions` is preserved as an alias for backwards-compatible muscle memory; SPEC-021 calls these
capability files."* The SDD proposes a THIRD, top-level `arc extensions` (4-family inspector). Three
commands all named around "extensions" is a UX trap — `ext` (author capability files), `agent
extensions` (list capability files), `extensions` (inspect extension-point families) are three different
concepts. **Recommend:** either fold the 4-family inspection into the existing `ext` command
(`arc ext inspect` / `arc ext families`), or give it a distinct name (e.g. `arc points`), and explicitly
reconcile the existing `agent extensions` alias. The SDD already leans on `arc ext`'s formatting helpers
(§6.1) — reusing that command is the lower-friction path. This needs a Josh call (new OQ).

**D-292 tension — the "extensions DISABLED at federal" posture (D-12) conflates two surfaces.** D-292
(decisions-log: *"Federal: extensions DISABLED. Enterprise: require approval. Personal: all enabled"*)
governs **agent self-modification** (the agent creating/altering its own tools/skills at runtime). The
select-one **BYO seam is different**: `_load_custom` today *permits* an operator-**allowlisted** BYO
brain/adapter even at federal (it only refuses *unsigned/non-allowlisted* dotted paths). So "extensions
disabled at federal" (self-modification) ≠ "operator BYO refused at federal" (the gate allows
allowlisted). D-12's claim that this posture is "surfaced as `RelaxableKnob`s and reflected by `arc
extensions verify`" risks making `arc extensions verify` wrongly report an allowlisted federal BYO as
"would be refused." **Recommend** D-12/§6.2 distinguish: (i) `arc extensions verify` reflects the real
BYO gate (allowlisted-OK at federal); (ii) D-292's self-modification disablement is a separate
arcskill/dynamic-tool concern (SPEC-044 territory), not the operator-configured select-one seam. Note
also: D-292's federal disablement is **not currently enforced** in the BYO path — flag whether SPEC-047
is expected to add it or leave it to the self-modification surface.

---

## 7. Data flow — `arc init --blueprint federal-analyst` (real path, AC-3/AC-4)

```
arc init --tier personal --blueprint federal-analyst
  → resolve_blueprint("federal-analyst", tier="personal")     # packaged, trusted
  → effective_tier = max(personal, federal) = federal          # REQ-013 stringency-max
  → merged = blueprint_overlay  (blueprint < user flags; but tier floored up to federal)
  → write ~/.arc/arcagent.toml with [security] tier=federal + [modules.memory] brain=arcmemory
  → emit blueprint.applied audit
  → later: load_config() → SecurityConfig.model_validator forces FIPS/vault_transit/ecdsa  # AC-4
  → ArcAgent.startup → modules.memory._runtime.configure → select_brain("arcmemory")
       → select_extension(_BRAIN_POINT, ...) → _build_arcmemory → ArcMemoryBrain active  # AC-3
```

Nothing in this path is a fixture: the blueprint is the packaged TOML, the merge is `load_config`, the
tier floor is the real `model_validator`, and the brain is the real `select_extension`.

### Research Insights (/deepen — CONFLICT #1: init tier vocabulary)

**The §7 example `arc init --tier personal` is INVALID against the real CLI.** `commands/init.py`
accepts tiers `_VALID_TIERS = ["open", "enterprise", "federal"]` (init.py:19-70); the interactive
`tier_map = {"1": "open", "2": "open", "3": "enterprise", "4": "federal"}` (init.py:265). There is **no
`personal` init tier** — the user-facing vocab is `open`/`enterprise`/`federal` (an arcllm-style axis
whose presets are arcllm sections: routing/telemetry/audit/…). The core `Tier` StrEnum, by contrast, is
`personal`/`enterprise`/`federal` with **no `open`**. So the SDD data-flow must use `--tier open` (or
`federal`), not `--tier personal`.

**But init ALREADY normalizes `open → personal` when writing arcagent's `[security] tier`** —
`init.py:172` writes `tier = "{tier if tier in ('federal','enterprise') else 'personal'}"` (gateway does
the same at :204). So the *written* `[security].tier` is always a valid `Tier` value; the conflict is a
**user-facing vocab fork**, not an invalid-config bug.

**Exact mapping fix (recommended):**
1. **Extract the duplicated inline normalization** (`init.py:172` + `:204`) into one helper, e.g.
   `_security_tier(init_tier) -> str` returning `init_tier if init_tier in ("federal","enterprise") else
   "personal"`. Required anyway because the blueprint merge needs the normalized tier in one place.
2. `arc init --blueprint` computes `effective = stringency_max(_security_tier(init_tier),
   blueprint.tier)` over the `Tier` axis and writes `[security].tier = effective`.
3. **Fix §7's example** to `arc init --tier open --blueprint federal-analyst` (→ `_security_tier` gives
   `personal`; stringency-max with `federal` → `federal`).
4. *Larger, cleaner alternative — Josh call:* rename init's user-facing `"open"` → `"personal"` to unify
   the vocab end-to-end. That touches the arcllm-preset axis (`open` keys arcllm.toml module presets), so
   it is outside SPEC-047's nominal scope and must NOT be done silently — surface as an OQ. Minimum-viable
   = steps 1-3 (normalize + fix the example), no rename.

**Writer-shape reminder (see §4 DC-8):** `arc init` emits TOML by f-string templating, so the
`--blueprint` deep-merge needs a structured assembly step (parse-merge-redump or dict-based rewrite);
there is no dict-merge hook in `init` today.

---

## 8. Security & threat mapping

| Threat | Mechanism |
|---|---|
| ASI04 supply chain (BYO extension = startup RCE) | REQ-002 refuse-before-import allowlist gate (unchanged from SPEC-041/044), now single-sourced |
| LLM03 supply chain (unsigned/wrong-key blueprint) | REQ-014 verify-before-use PINNED to the operator pubkey; above-personal an unsigned, tampered, or wrong-key preset is refused fail-closed, and an unresolvable operator key denies (an unpinned floor is no floor) |
| ASI04 supply chain (false "signed" display) | `arc ext inspect`/`verify` pins scan-many `.arcsig` to the agent DID key, so a wrong-key self-signed capability reads "unsigned" (mirrors the live loader's verdict) |
| ASI01/ASI03 privilege via config | REQ-013 tier floor non-relaxable through a blueprint; effective-tier stringency-max |
| ASI06 config poisoning | blueprints are operator-applied CLI artifacts, never agent-writable; audited (REQ-015) |
| AU-9/AU-10 audit integrity | apply + relaxation events to operator-signed WORM sink (SPEC-053) |
| LLM06 excessive agency | `arc extensions verify` surfaces what would load; scan-many signature floor unchanged |

### Research Insights (/deepen — prior-art adoption decisions)

**1. Plugin discovery — refuse-before-import is VALIDATED prior art, entry_points is the anti-pattern.**
"Command-jacking" / entry-point hijack is a documented supply-chain technique: any installed
distribution can register an `importlib.metadata` entry point, and calling `.load()` on it **executes
that package's code at import time** — the exact startup-RCE (ASI04/LLM03) Arc's `_load_byo`
refuse-before-import gate prevents. `pluggy` (pytest/tox) has **no trust model** — it manages hook
ordering, not provenance. **Adopt:** keep Arc's explicit allowlist + verify-before-import; do NOT add
`entry_points` auto-discovery for extension points. (Checkmarx command-jacking:
https://checkmarx.com/blog/this-new-supply-chain-attack-technique-can-trojanize-all-your-cli-commands/;
Python packaging security: https://cheatsheetseries.owasp.org/cheatsheets/NPM_Security_Cheat_Sheet.html)

**2. Signed-preset precedence + verification — Helm is the closest analog; adopt its bundle+gate.**
Helm ships a chart's provenance as a detached bundle (metadata + SHA-256 + PGP signature); `helm verify`
/ `--verify` **fails closed before render** — verification is a hard gate, not advisory. Values
precedence is chart-defaults < `values.yaml` < `--set` (a *starting point* the user overrides) — exactly
Arc's intended blueprint < user-config ordering. **Adopt:** blueprint = TOML + `.arcsig` sidecar
(Arc's Helm-bundle equivalent), verified fail-closed **before** merge; blueprint is the lowest
write-time layer. Ansible Galaxy makes collection GPG-signing **mandatory** with any bypass an *explicit,
audited* flag — mirror that: above personal, unsigned = refused, never silently. Terraform's historical
**unsigned-modules gap** (only *providers* are signature-checked) is the pitfall to avoid — sign the
*preset*, not just the engine. (Helm provenance:
https://developer.harness.io/release-notes/helm-chart-provenance/, verify:
https://colinwilson.uk/2022/02/07/verifying-signed-helm-charts/; Ansible signing:
https://docs.ansible.com/projects/galaxy-ng/en/latest/config/collection_signing.html; Terraform signing:
https://developer.hashicorp.com/terraform/cli/plugins/signing)

**3. Selection is orthogonal to precedence; wheel-provenance trust is bounded.** VS Code Profiles teach
that choosing a preset (which blueprint) is a **separate axis** from how its values merge — Arc should
keep "which blueprint" (a CLI selection) distinct from "blueprint-vs-user precedence" (the write-time
merge order), which OQ-10 pins. Sigstore/cosign `verify-bundle` is the model for detached
verify-before-use of config/OCI artifacts; the policy-controller **TOCTOU** lesson → pin blueprints by
**content hash, not a mutable version tag**. "Trusted by provenance" (packaged presets shipped read-only
in the signed wheel, unsigned) is **sound ONLY for content that never leaves the wheel** — the moment a
preset enters from `~/.arc/blueprints/` or a third-party pack it needs its own `.arcsig`. This exactly
validates OQ-3's split (packaged = provenance-trusted; user/external = signature-required). (cosign
bundles: https://blog.sigstore.dev/cosign-verify-bundles/; VS Code profiles:
https://code.visualstudio.com/docs/configure/profiles)

---

## 9. Testing strategy (see PLAN for ACs)

- **Unit:** `select_extension` dispatch table (none/builtin/auto/BYO × personal/enterprise/federal);
  `resolve_tier_floor` floor + relaxation + audit; blueprint merge precedence + stringency-max tier.
- **Security:** BYO refuse-before-import with an import-side-effect sentinel (AC-2); unsigned-blueprint
  refusal above personal (AC-5); federal-floor-holds-through-blueprint (AC-4).
- **Integration/E2E (producers-unwired defense, REQ-041):** `arc init --blueprint` → real
  `load_config` → real `ArcAgent` boot → assert concrete selected Brain (AC-3); `arc extensions`
  reflects the real registry (AC-6).
- **Regression:** the entire existing `brain/` + `skilladapt/` + capability-loader suites pass
  unchanged bar enumerated import edits (AC-1).

---

## 10. Component → requirement map

| Component | REQ |
|---|---|
| `extension/point.py`, `extension/select.py` | 001, 002, 005 |
| `brain/select.py`, `skilladapt/select.py` refactor | 003, 004 |
| `extension/families.py` | 006 |
| `extension/inspect.py` | 006, 030, 031 |
| `blueprints/loader.py` + packaged `*.toml` | 010–017 |
| `arcagent/tiers.py` | 020–023 |
| `core/config.py` validator delegation | 021, 022 |
| `arccli/commands/blueprint.py` | 011, 015, 016, 017 |
| `arccli/commands/extensions.py` | 030, 031 |
| `arccli/commands/init.py` (+`--blueprint`) | 011, 012, 013 |
| `arccli/commands/registry.py` | 032 |
