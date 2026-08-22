# arctrust

> **Build standards:** repo root [`AGENTS.md`](../../AGENTS.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Cryptographic leaf of Arc: DID identity, Ed25519 signing, fail-closed policy, and audit emission. Every other package depends on these primitives; nothing here depends on another Arc package.

## Layer

**Trust foundation (leaf).** Imports: PyNaCl, Pydantic, cryptography only — **no Arc imports** (enforced by `tests/test_layering.py`).

## Layout

```
src/arctrust/
  identity.py         # AgentIdentity, ChildIdentity, DID
  keypair.py          # Ed25519 KeyPair, sign/verify
  operator.py         # OperatorKey (audit authority, ≠ agent identity)
  users.py            # Human user identities (User, UserStore, OPERATOR/VIEWER)
  session_identity.py # The ONE conversation/session-key derivation
  signer.py           # Signer seam — in-process / vault-transit, ed25519 / ecdsa-p256
  fips.py             # FIPS gate (signing + encryption)
  policy.py           # PolicyPipeline, Decision, ToolCall — first-DENY, fail-closed
  classification.py   # Classification ladder + no-read-up helpers
  audit.py            # AuditEvent, sinks (WormSink), emit(), verify_chain
  audit_cipher.py     # RecordCipher — seals WORM record content at rest (D-577)
  artifact.py         # Detached artifact signing / verification
  canonical.py        # canonical_json — reuse for anything signed
  tofu.py             # Trust-on-first-use capability-source gate
  validators.py       # Persisted source approvals ([security.validators])
  trust_store.py      # Operator / issuer pubkey loaders + register_operator
  redaction.py        # PII/secret detection + redact_text
  secrets.py          # SECRET_PATTERNS (structured-prefix secret scanning)
  witness.py          # External witness anchors + divergence detection
  paths.py            # THE resolver: ~/.arc (runtime/config/state) + fleet outside it
  home_migration.py   # One-time move of a flat ~/.arc into the split layout
  _notary.py          # Reference out-of-process notary (child of FileNotaryTransit)
```

## Entry points

Public surface is `arctrust` (`__init__.py`): `AgentIdentity`, `KeyPair`, `sign`/`verify`, `OperatorKey`, `User`/`UserStore`, `Signer`/`build_signer`, `PolicyPipeline`/`build_pipeline`, `AuditEvent`/`emit`/`WormSink`, `RecordCipher`, TOFU helpers, `redact_text`/`SECRET_PATTERNS`, `canonical_json`, the `arctrust.paths` accessors, and FIPS/witness utilities. (`session_identity` is imported as `arctrust.session_identity`, not re-exported at the root.)

Docs: `packages/arctrust/README.md`, repo `docs/trust-model.md`. Four Pillars (ADR-019) live here.

## Package rules

- **Never** add an import of another Arc package.
- **Never compose an Arc-home path by hand.** `arc_home() / "operator"` reads the
  pre-split location; after a migration that silently loses the operator signing
  key, and every WORM chain it signed becomes unverifiable. Call the accessor
  (`trust_dir()`, `config_file("arcagent.toml")`, `module_root()`, …) — every one
  takes an optional explicit base for `--arc-dir`. Enforced by
  `tests/architecture/test_arc_home_single_resolver.py`; `paths.py` and
  `home_migration.py` are the only exempt files.
- `paths.py` resolves the environment **per call**, never at import: `ARC_CONFIG_DIR`
  is routinely exported after the module loads.
- Identity, Sign, Authorize, Audit belong here — do not reimplement upstairs.
- Operator key ≠ agent identity; keep that distinction sharp.
- Anything hashed for signing must go through `canonical_json` (or the established artifact path), not ad-hoc dumps.

## Tests

`packages/arctrust/tests/` — identity, policy layers, TOFU, FIPS, WORM, signing tier matrix, layering/boundary.

## Working here

Change a pillar → update callers carefully; this package is the shared root. Prefer extending existing emit/pipeline seams over new parallel channels.