# ADR-005 — Vault backend `**kwargs` forwarding contract

**Status:** Accepted (2026-05-06)
**Spec:** SPEC-025 (AWS Secrets Manager backend) + future backends
**Pillar trace:** Simplicity, Modularity

## Context

`packages/arcllm/src/arcllm/vault.py:VaultResolver.from_config()` is the
single entry point that constructs a backend from operator-supplied TOML
config. Until SPEC-025, it called `backend_class()` with no arguments —
which meant any backend that needed configuration (region, URL,
namespace, profile, etc.) had to read from env vars or static defaults.

When SPEC-025 added `AwsSecretsManagerBackend`, two real config knobs
emerged: `region_name` (operator may want non-default region) and
`profile_name` (dev convenience). To pass them through cleanly without
hardcoding "AWS-only" forwarding logic, `from_config()` was extended to
accept `**backend_kwargs` and forward verbatim to the backend
constructor. `arcllm/registry.py` reads `vault_cfg.region` and
`vault_cfg.url` from the existing `VaultConfig` model and passes them
through only when truthy (so backends that don't accept them never see
empty-string spurious kwargs).

The architecture review (Phase 2 §arch-M-3) flagged the blast radius:
a future backend (`HashicorpBackend`, `GcpSecretManagerBackend`, etc.)
that doesn't accept `region_name` will raise `TypeError` from the
forwarded call. This is fail-LOUD, not fail-silent — but it means
adding a new optional field to `VaultConfig` is a breaking change for
every backend that pre-dates the field.

## Decision

**Forward `**backend_kwargs` verbatim. Backends fail loud on unknown
kwargs (TypeError).**

The alternatives considered:

1. **Backends accept and silently ignore unknown kwargs** —
   `def __init__(self, **kwargs)` then pull what they recognise. Rejected:
   silent ignore hides operator typos (`reigon` → ignored → backend
   uses default region while the auditor sees `region` in the config).
   Loud failure surfaces misconfiguration at startup.

2. **Per-backend kwarg whitelist in `registry.py`** — registry knows
   that `AwsSecretsManagerBackend` accepts `region_name` but not `url`,
   etc. Rejected: violates the modularity boundary (registry would have
   to track every backend's signature) and creates tight coupling.

3. **Status quo (no forwarding)** — operator sets `[vault] region` in
   TOML and arcllm silently ignores it. Rejected: that was the bug we
   set out to fix.

## Consequences

**Positive:**
- Misconfigured TOML (typo'd kwarg, kwarg meant for the wrong backend)
  surfaces at startup, before any secret is fetched.
- New backends opt in to whatever config they accept without registry
  changes.
- The registry-side guard (`if vault_cfg.region:`) means missing fields
  don't propagate as `region_name=""` to backends; only set values flow.

**Negative:**
- Adding a new optional field to `VaultConfig` requires either
  (a) coordinating an update to every backend that the deployment uses,
  or (b) making the new field's registry-side forwarding gated more
  narrowly (per-backend allow-list).
- A future deployment that hot-swaps from `AwsSecretsManagerBackend` to
  `HashicorpBackend` and forgets to remove `region` from `[vault]` will
  TypeError at startup — this is the intentional loud-fail behavior.

## Implementation pointers

- `packages/arcllm/src/arcllm/vault.py:from_config(..., **backend_kwargs)`
- `packages/arcllm/src/arcllm/registry.py` — only forwards kwargs that
  are truthy on the operator's `VaultConfig`.
- `packages/arcllm/src/arcllm/backends/aws_secrets.py` — keyword-only
  init (`region_name`, `profile_name`) so accidental positional kwargs
  fail loud.

## Verification

- `tests/test_aws_secrets_backend.py::TestConstructorAcceptsKwargs::test_kwargs_only_init_does_not_raise`
- `tests/test_vault.py::test_from_config_forwards_kwargs` (to be added in
  the next test pass — backwards-compat with no-kwargs call sites is
  exercised by the existing `test_vault.py::test_from_config_*` cases).

## Future work

If the kwarg-mismatch failure mode bites in practice, the fallback path
is per-backend `allowed_kwargs` set on the class (a class-level frozenset
declaring which keys it accepts; registry filters before forwarding).
That would convert the loud-fail to silent-ignore. Defer until empirical
evidence supports the change — operators currently benefit from the
loud-fail.
