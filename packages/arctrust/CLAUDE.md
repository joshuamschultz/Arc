# arctrust

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Cryptographic leaf of Arc: DID identity, Ed25519 signing, fail-closed policy, and audit emission. Every other package depends on these primitives; nothing here depends on another Arc package.

## Layer

**Trust foundation (leaf).** Imports: PyNaCl, Pydantic, cryptography only — **no Arc imports** (enforced by `tests/test_layering.py`).

## Layout

```
src/arctrust/
  identity.py       # AgentIdentity, ChildIdentity, DID
  keypair.py        # Ed25519 KeyPair, sign/verify
  policy.py         # PolicyPipeline, Decision, ToolCall — first-DENY, fail-closed
  audit.py          # AuditEvent, sinks, emit()
  operator.py       # OperatorKey (≠ agent identity)
  signer.py         # Signing helpers
  tofu.py           # Trust-on-first-use layer
  artifact.py       # Signed artifact verification
  canonical.py      # canonical_json — reuse for anything signed
  classification.py # Classification-aware helpers
  fips.py           # FIPS mode helpers
  witness.py        # Witness / attestation
  trust_store.py    # Trust store loaders
  validators.py     # Shared validators
  paths.py          # Path helpers
```

## Entry points

Public surface is `arctrust` (`__init__.py`): `AgentIdentity`, `KeyPair`, `sign`/`verify`, `PolicyPipeline`/`build_pipeline`, `AuditEvent`/`emit`, TOFU helpers, `canonical_json`, FIPS/witness utilities.

Docs: `packages/arctrust/README.md`, repo `docs/trust-model.md`. Four Pillars (ADR-019) live here.

## Package rules

- **Never** add an import of another Arc package.
- Identity, Sign, Authorize, Audit belong here — do not reimplement upstairs.
- Operator key ≠ agent identity; keep that distinction sharp.
- Anything hashed for signing must go through `canonical_json` (or the established artifact path), not ad-hoc dumps.

## Tests

`packages/arctrust/tests/` — identity, policy layers, TOFU, FIPS, WORM, signing tier matrix, layering/boundary.

## Working here

Change a pillar → update callers carefully; this package is the shared root. Prefer extending existing emit/pipeline seams over new parallel channels.
