# arcbundle

> **Build standards:** repo root [`AGENTS.md`](../../AGENTS.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Signed module bundles: the on-disk distribution unit that makes a module's **absence** provable. The `arc-agent` wheel carries no module code and is byte-identical across every tier; each module ships as a separately signed `.arcbundle`, verified in full before a single byte reaches disk, materialized read-only at the deployment root outside every agent's tool fence, then copied per agent as tools + skills.

## Layer

**Leaf, beside `arctrust`.** Imports `arctrust` (Ed25519 + `canonical_json`) and Pydantic — **nothing else, ever**. It knows nothing of `arcagent`: the agent only ever READS an already-materialized directory, so the nucleus stays ignorant of distribution and a bundle can be built or verified on a low-side box with no agent stack present.

## Layout

```
src/arcbundle/
  manifest.py         # BundleManifest / FileEntry — the signed on-disk shape + canonical_bytes
  signer.py           # sign_manifest — detached Ed25519 over a manifest's canonical bytes
  verifier.py         # verify_bundle — fail-closed: signature → canonical form → every file hash → no undeclared files
  materializer.py     # materialize / remove — atomic write (stage → fsync → rename), 0444 files in 0555 dirs
  builder.py          # build_bundle — package a module folder, sign each .py + SKILL.md with a .arcsig sidecar
  capability_copy.py  # copy_capabilities / remove_capabilities — per-agent copy of a module's tools + skills
  _audit.py           # one emitter per decided outcome; sinks fan out via arctrust.audit.emit
  _paths.py           # require_safe_name / require_relative_path — the write-primitive guards
  errors.py           # BundleError + one subclass per failed gate (NOT rooted in ValueError, on purpose)
```

## Entry points

Public surface is `arcbundle` (`__init__.py`): `build_bundle`, `verify_bundle`/`VerifiedBundle`, `materialize`/`remove`, `sign_manifest`, `BundleManifest`/`FileEntry`, `copy_capabilities`/`remove_capabilities`/`capability_dir`, the `TIERS`/`DEV_ISSUER` constants, and the `BundleError` family.

## Package rules

- **Never import an Arc package other than `arctrust`.** The leaf property is what lets a bundle build and verify with no agent stack — break it and you lose the low-side/air-gap story.
- **Nothing is written before everything is verified.** A failed verify leaves the destination byte-identical; verification never mutates the filesystem.
- **Fail closed.** Any exception during verification denies. `_verify` has one `return`, on its last line.
- **Verified bytes travel in memory** (`VerifiedBundle.files`) — never re-read from the bundle dir at materialize time. That closes the gap between "hashed correctly" and "written".
- **Module runtime is never agent-writable** — `0444` files in `0555` dirs, at the deployment root outside the tool fence. `_runtime.py` is never copied to an agent.
- **Every `.py` and every `SKILL.md` gets a `.arcsig`**, not only `capabilities.py`: the loader imports each `.py` (an import runs the file) and injects `SKILL.md` text into the prompt.
- **Two signatures, one key.** Manifest sig = *this bundle is what its issuer built* (checked once at install). Per-file `.arcsig` = *this file is what its issuer signed* (checked on every capability scan). Sidecars ride inside the payload, so a swapped one is a content-hash mismatch, never a new trust anchor.
- **The dev issuer (`did:arc:dev`) is trusted at personal tier only**, pinned in the verifier — a CLI flag can't widen it.
- **Path/name guards live where the value is constructed** (`_paths.py`, model validators), not where it is used — no caller can route around them. Refusals are not rooted in `ValueError` so a Pydantic validator can't swallow them into a `ValidationError`.

## Audit

`module.bundle.verified`, `module.signature_invalid`, `module.content_hash_mismatch`, `module.installed`, `module.removed` — each emitted from inside this package at the point the outcome is decided. Pass `sink=` and `actor_did=` to `verify_bundle`, `materialize`, and `remove`; sinks fan out from `arctrust.audit.emit`. Audit failures are swallowed and logged, never allowed to break the install (NIST AU-5).

## Tests

`packages/arcbundle/tests/` — manifest/signer/verifier gates, atomic materialize + remove, builder round-trip through the verifier, per-agent capability copy, path-guard and tier matrix.

## Working here

A new gate goes in `verifier.py` before the single `return`, catching precisely what it can provoke. Any new payload class that the capability loader executes or injects must gain a `.arcsig` in `builder._is_adjudicated_artifact`. Keep every path join behind `_paths.py` — a manifest string reaching a deployment-root join unguarded is an arbitrary-write primitive (ASI05/ASI06).