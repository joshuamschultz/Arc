# ADR-033: A module's dependency contract is its `configure()` signature, not a core registry

**Status**: Accepted
**Date**: 2026-08-18
**Supersedes**: the hardcoded `_RUNTIME_SPECS` registry and the `RuntimeModuleSpec` type in `arcagent.core` (deleted here)

## Context

Arc's seam model has one non-negotiable: **the core names no part.** A module is
discovered by presence on disk and activated by config; nothing in core should
enumerate module names. Modules are meant to be removable, swappable, and — for
third parties — addable without editing core.

The module runtime seam violated this. `arcagent/core/agent_lifecycle.py` held
`_RUNTIME_SPECS`, a hardcoded dictionary mapping all eighteen module names to a
`RuntimeModuleSpec` — the exact tuple of privileged dependency keys each module's
`configure()` was allowed to receive (`operator_signer`, `human_gate`,
`policy_pipeline`, `tool_registry`, and the rest). A module with no entry raised
`"has no runtime dependency contract"` at startup, so **a new module — first- or
third-party — could not load without a core edit.** That is the one thing the
doctrine forbids, sitting in the middle of core.

The dependency keys are already the `configure()` keyword-parameter names
(`DependencyKey.OPERATOR_SIGNER = "operator_signer"`, etc.), and
`docs/building/modules.md` already documented the intended design as
"deliver only the dependencies the module's `configure()` names as parameters."
The code had drifted to a redundant second source of truth. A characterization
check across all eighteen shipped modules confirmed that
`set(configure parameters) ∩ vocabulary` reproduces every `_RUNTIME_SPECS` entry
**exactly** — the registry carried no information the signature did not.

## Decision

**A module declares its dependency contract by naming keyword parameters on
`configure()` that match the closed `DependencyKey` vocabulary. Core offers the
full dependency menu and hands each module exactly the entries it names.**

`RuntimeDependencies.select_for(configure, module_config)` builds the menu once,
reads the target's signature, and returns `{name: value}` for every parameter in
the vocabulary — nothing else. `config` is the module's own
`[modules.NAME.config]` table; every other key mirrors the field of the same name
on `RuntimeDependencies`. Parameters outside the vocabulary (a module's own
injected test helpers, e.g. `tasks`' `messenger`/`registry`) keep their defaults.

`_RUNTIME_SPECS`, the `RuntimeModuleSpec` type, and its `optional` flag are
**deleted outright, not deprecated.** Every enabled module remains required: a
`configure()` failure aborts startup with the cause chained, because
half-configuring the agent and continuing hides the failure until some later tool
silently no-ops. (`optional` was never set `True` by any shipped module, so no
behavior is lost.)

## Consequences

- **The core names no module.** `agent_lifecycle.py` holds no per-module data;
  adding a module — including a third-party signed bundle — needs zero core edits.
  This is enforced by `test_unknown_module_configures_with_no_core_registration`.
- **Least privilege is preserved, and its source of truth is singular.** A module
  receives only the privileged dependencies it names; a module that never writes
  `operator_signer` in its signature never gets it (SPEC-037, ASI03). The signature
  is now the *only* place that grant is expressed, so the spec and the signature can
  no longer disagree.
- **The trust boundary is unchanged.** *Which* modules load is still governed by
  discovery + config activation + signed-bundle verification (a `module:` root is
  `VERIFIED` at every tier). Naming a privileged dependency in a signed module is
  the operator approving that grant by signing the bundle — the same authorization
  the hardcoded registry encoded, moved to where the module is actually reviewed.
- **A renamed or unknown dependency still fails loudly.** A keyword-only parameter
  with no default that names nothing in the vocabulary leaves `configure()` missing
  a required argument, which aborts the required module rather than silently
  skipping it.
- **Core shrinks.** ~55 lines of registry plus the `RuntimeModuleSpec` type are
  gone, replaced by one `select_for` method — net negative against the core LOC
  budget (ADR-004).
- **Docs are now true, not aspirational.** `docs/building/modules.md`'s
  signature-based description and the "core names no part" invariant in
  `docs/concepts/seam-model.md` describe the running code.
