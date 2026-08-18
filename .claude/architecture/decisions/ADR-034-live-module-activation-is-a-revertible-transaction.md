# ADR-034: Live module activation is a revertible transaction

**Status**: Accepted
**Date**: 2026-08-18
**Relates to**: ADR-023 (capability resolution, layered scan roots, transactional reload), ADR-033 (a module declares its dependencies by signature)

## Context

A module contributes tools, hooks, background tasks, and per-agent runtime
state. Until now those effects were bound once, at `startup()`, and there was no
inverse: enabling or disabling a module, or upgrading its code, meant a full
agent restart. The seam model promises that a part can be turned off, swapped,
or specialized per deployment; a restart-only lifecycle makes that a heavy,
whole-agent operation rather than a local one.

The machinery to unbind effects already existed but was not composed into a
module-level operation. The capability reload (`agent.reload()`,
`CapabilityLoader.prepare_reload`/`commit_reload`) is already transactional: it
re-scans the loader's scan roots into an isolated candidate registry and swaps it
in atomically, so a tool or hook whose scan root is gone is removed, and one
whose root appears is registered. The Module Bus already unsubscribes and
atomically replaces handlers by module prefix. Background tasks already
drain-then-replace. What was missing was a single call that drives all of them
for one module, plus an inverse for a module's *own* resources — a live
connection, a self-spawned task — which the capability registry does not own.

## Decision

**Enabling or disabling a module at runtime is one revertible transaction,
`agent.set_module_enabled(name, enabled=…)`, with no restart.**

- The **capability half** rides the existing transactional reload. The loader
  grows one method, `set_module_roots(agent_dir, module_names)`, that swaps the
  `module:*` scan roots and leaves every other root untouched (module roots stay
  last, preserving last-wins precedence). `set_module_enabled` updates that set
  and calls `agent.reload()`; the rescan registers or removes the module's tools,
  hooks, and background tasks. No new capability-swap path is introduced.
- The **runtime half** is the module's own `configure()` on enable — from the
  same dependency menu a startup activation uses, kept on the agent as
  `_runtime_deps` (ADR-033) — and an optional `teardown()` on disable, a new
  `RuntimeTeardownable` protocol. `teardown` is opt-in: a module implements it
  only for resources the capability registry does not already own. A module
  without it is still fully unwound — its capabilities are removed by the reload
  and its task-local binding is dropped.
- The **task-local binding** (the per-turn `RuntimeBinding` rebind) is appended
  on enable and removed on disable, so a disabled module's state is never rebound
  into a later turn.

`set_module_roots` lives in `arcagent/capabilities/` (not `core/`), so the
capability-side logic stays out of the core LOC budget; only the small
orchestrator and the `RuntimeTeardownable` protocol are in core.

## Consequences

- **A module is safe to turn off, swap, or upgrade in place.** "Survive its own
  absence" now holds live, mid-run, not only across a restart — the property the
  seam model asserts, made real.
- **No second swap mechanism to secure.** Disable/enable reuses the reload that
  already carries the signature gate, the isolated-candidate safety (an invalid
  candidate never damages the working set), and the audit events. A hot-swap is
  as trustworthy as a reload because it *is* a reload.
- **Least privilege and trust are unchanged.** A re-enabled module is configured
  from the same dependency menu and its `module:` scan root is still `VERIFIED`
  (signature required at every tier). Nothing about which effects a module may
  bind changes because it was bound late.
- **`teardown` is optional, so adoption is incremental.** Modules that hold a live
  resource (e.g. a messaging NATS consumer) implement `teardown` to release it on
  disable; the rest need no change. Wiring `teardown` into each such module is
  follow-up, not a precondition for the contract.
- **Known limitation.** The runtime-half mutation and the reload are not taken
  under a single lock, so a turn dispatched concurrently with a hot-swap can
  observe an in-between state. Hot-swap is an operator action, not a hot path;
  tightening this to hold `_reload_lock` across the whole transaction is a
  follow-up if contention is ever observed.
