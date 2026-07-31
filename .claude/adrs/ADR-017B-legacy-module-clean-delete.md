---
id: ADR-017B
title: Delete Legacy pulse + scheduler Modules Outright
status: accepted
date: 2026-04-18
spec: SPEC-017
tags:
  - migration
  - modularity
  - breaking-change
---

# ADR-017B: Delete Legacy `pulse` + `scheduler` Modules Outright

## Status

Accepted (2026-04-18). `modules/pulse/` and `modules/scheduler/` deleted; replaced by `modules/proactive/`.

## Context

SPEC-017 R-040 mandates that `modules/pulse/` and `modules/scheduler/` be replaced by a single unified `modules/proactive/` module. Three migration shapes were considered:

1. **Hard delete** — remove the old modules, test suite, and state. One commit.
2. **Deprecation shim** — keep the old modules on disk with stub imports that redirect to `proactive/`, deprecated-in-docstring.
3. **Parallel coexistence** — both modules ship; users opt into the new one via config.

The CLAUDE.md project standard explicitly disallows backwards-compatibility shims ("No shortcuts that trade security for convenience").

## Decision

**Option 1: hard delete.** In the SPEC-017 implementation commit:

- Remove `packages/arcagent/src/arcagent/modules/pulse/` entirely
- Remove `packages/arcagent/src/arcagent/modules/scheduler/` entirely
- Remove `packages/arcagent/tests/unit/modules/pulse/`, `tests/unit/modules/scheduler/`
- Remove `packages/arcagent/tests/integration/test_cron_*`, `test_scheduler_integration.py`
- Update `test_base_config.py` to drop now-invalid scheduler config tests
- CHANGELOGs document the migration procedure (export + re-import)

External modules that satisfied the old `DeliverySender` Protocol (notably `arcgateway/delivery.py`) keep their implementation — the class is still useful standalone. Only the docstring Protocol reference moves.

## Consequences

### Positive

- **Clean break** — no dead code paths lingering in the repo.
- **Reduced test runtime** — 20+ scheduler tests removed; overall suite runs faster.
- **Zero ambiguity** — there's exactly one way to schedule work in the new world.

### Negative

- **Upgrade path requires manual migration** — any deployment with persisted schedule state under `~/.arcagent/scheduler/` must run `arc agent schedule migrate` before the new engine takes over.
- **External consumers of the deleted Protocol break** — no in-tree consumers exist, but third-party extensions referencing `arcagent.modules.scheduler.DeliverySender` will raise `ModuleNotFoundError`.

### Mitigations

- Runbook at `packages/arcagent/docs/runbooks/spec-017-operations.md` documents the migration.
- `arc agent schedule migrate` CLI ships alongside the new engine (SPEC-017 task 6.22).
- CHANGELOG lists the breaking change prominently.

## Alternatives considered

- **Option 2 deprecation shim** — rejected. Violates CLAUDE.md project standard. Leaves dead code for months.
- **Option 3 parallel coexistence** — rejected. Two schedulers in one process will compete for leader election, duplicate dispatches, and fragment the audit trail.

## References

- SPEC-017 R-040
- CLAUDE.md "No shortcuts that trade security for convenience"
- Runbook: `packages/arcagent/docs/runbooks/spec-017-operations.md`
