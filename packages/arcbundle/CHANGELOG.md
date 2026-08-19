# Changelog

All notable changes to arcbundle will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] - 2026-08-19

SPEC-066 signed module bundles: the distribution unit that makes a module's
absence provable. The `arc-agent` wheel carries no module code; each module
ships as a separately signed bundle, verified in full before a byte reaches
disk, materialized read-only outside every agent's tool fence, then copied per
agent as tools + skills. Leaf beside `arctrust` — no agent stack required to
build or verify.

### Added
- **`BundleManifest` / `FileEntry`** (`manifest.py`) — the signed on-disk shape.
  `canonical_bytes()` delegates to `arctrust.canonical_json`, the one serializer
  a signature binds to, so signer and every verifier agree on the same bytes.
  Model validators reject a non-canonical digest spelling, a duplicate path, an
  unknown `format_version`, and an unsafe module name at construction.
- **`sign_manifest`** (`signer.py`) — detached Ed25519 over a manifest's
  canonical bytes. Accepts a raw seed or an `arctrust.Signer`; release CI and
  `--from-source` share the one code path with a different key.
- **`verify_bundle` / `VerifiedBundle`** (`verifier.py`) — fail-closed verify in
  order: issuer trusted for the tier → signature → canonical form → every
  declared file hash → refusal of any undeclared or symlinked payload file. No
  filesystem mutation on any path. Verified payload bytes are carried in memory
  so what is written is exactly what was verified. `issuer_key` travels on the
  result so an installer pins the key that actually passed.
- **`materialize` / `remove`** (`materializer.py`) — atomic write (stage → fsync
  → rename with backup/restore on a failed publish), `0444` files inside `0555`
  directories at the deployment root. `remove` restores write permission across
  the tree and raises if any part survives.
- **`build_bundle`** (`builder.py`) — package a module source folder into a
  signed `.arcbundle` on the low side (no network, no agent stack). Skips build
  artifacts, never follows symlinks, and round-trips through the verifier's own
  layout. Signs each adjudicated artifact — every `.py` and every `SKILL.md` —
  with a `.arcsig` sidecar carried inside the signed payload.
- **`copy_capabilities` / `remove_capabilities` / `capability_dir`**
  (`capability_copy.py`, T-967..T-971) — per-agent copy of a module's tools +
  skills into `<agent_dir>/capabilities/modules/<module>/`, normalizing modes so
  the copy stays removable and signable. Copies signature sidecars along with
  their artifacts; never copies `_runtime.py`. The `modules/` namespace keeps a
  module named `skills` from colliding with the conventional loader root.
- **VERIFIED trust class for module capabilities** (T-972) — the per-agent copy
  is adjudicated as VERIFIED: a valid signature is mandatory at every tier, on
  every scan, because it sits in a directory the agent can write.
- **Bundle-lifecycle audit** (`_audit.py`) — `module.bundle.verified`,
  `module.signature_invalid`, `module.content_hash_mismatch`,
  `module.installed`, `module.removed`, each emitted from inside this package at
  the point the outcome is decided; sinks fan out via `arctrust.audit.emit`.
  Audit failures are swallowed and logged, never allowed to break the install
  (NIST AU-5).
- **Path/name write-primitive guards** (`_paths.py`) — `require_safe_name` and
  `require_relative_path` applied where a value is constructed, refusing `..`,
  absolute paths, drive/UNC prefixes, backslashes, and non-normalized forms so a
  manifest string can never become an arbitrary write against the installing
  user (ASI05/ASI06).
- **Refusal vocabulary** (`errors.py`) — `BundleError` plus one subclass per
  failed gate, deliberately not rooted in `ValueError` so a Pydantic validator
  cannot convert a refusal into a `ValidationError`. Every one means the same
  thing: nothing was installed.
