# arcbundle

Signed module bundles for Arc — the distribution unit that lets a deployment
contain exactly the modules it was approved for, and nothing else.

## Why

`pip install arc-agent` used to write every module to disk regardless of tier, so
a federal enclave that must not run the browser module still had its source on
the box. Extras gate dependencies, never files, and no packaging flag installs a
subset of a wheel. arcbundle makes the wheel carry no module code at all: each
module ships as a separately signed bundle, verified before a single byte is
written, and materialized read-only outside every agent's reach.

The result an operator can point at: absence is a directory listing, not a config
flag someone could flip.

## Layer

**Leaf, beside `arctrust`.** Imports `arctrust` (Ed25519 + canonical JSON) and
Pydantic — nothing else, ever. It knows nothing about `arcagent`: the agent only
reads an already-materialized directory, which keeps the nucleus ignorant of
distribution entirely.

A bundle can therefore be built and verified on a low-side staging box that has
no agent stack installed.

## Surface

| Module | Responsibility |
|--------|----------------|
| `manifest` | `BundleManifest` model + canonical-JSON encoding — the on-disk shape |
| `signer` | Detached Ed25519 signature over a manifest's canonical bytes |
| `verifier` | Fail-closed verify: manifest signature, then every declared file hash |
| `materializer` | Atomic write (stage → fsync → rename), `0444` files in `0555` dirs |
| `builder` | Package a module folder into a signed `.arcbundle`, signing each adjudicated artifact with a `.arcsig` sidecar |
| `capability_copy` | Per-agent copy of a module's tools + skills, and its inverse |
| `_audit` | One emitter per decided outcome; sinks fan out via `arctrust.audit.emit` |

## Where a module lands

Two destinations, two trust properties, no overlap:

| What | Where | Who may write it |
|------|-------|------------------|
| Runtime (`_runtime.py` and its support) | `${ARC_CONFIG_DIR:-~/.arc}/modules/<name>/` | The operator install only — `0444` in `0555`, outside the tool fence |
| Capability surface (`capabilities.py`, `skills/`) | `<agent_dir>/capabilities/modules/<name>/` | The agent — which is why the loader adjudicates it as verified and requires a valid signature at every tier |

`capability_copy` copies only the second, per agent, and normalizes the modes on
the way: a copy that inherited the deployment tree's read-only bits could
neither be removed nor signed in place.

## Two signatures, one key

The **manifest** signature says *this bundle is what its issuer built*. It is
checked once, at install, and the bundle directory can be deleted afterwards.

A per-file **`.arcsig`** sidecar beside each adjudicated artifact says *this file
is what its issuer signed*. That is the question the capability loader asks on
every scan, long after the bundle is gone: a `module:*` scan root is verified,
not trusted, so an artifact whose bytes changed since it was signed stops
loading. `builder` writes those sidecars into the payload, so they are covered by
the manifest signature and by the verifier's refusal of undeclared files — a
sidecar swapped in transit is a content-hash mismatch, never a new trust anchor.

Every `.py` is signed, not only `capabilities.py`: the loader imports each `.py`
at a module root looking for decorated values, and an import runs the file.
`SKILL.md` too — its text is injected into the agent's prompt.

The key that verified the manifest travels on `VerifiedBundle.issuer_key`, so an
installer pins the key that actually passed rather than one looked up again by
name.

## Audit

`module.bundle.verified`, `module.signature_invalid`,
`module.content_hash_mismatch`, `module.installed`, `module.removed` — each
emitted from inside this package at the point the outcome is decided, so every
surface that installs a bundle records the same fact. Pass `sink=` and
`actor_did=` to `verify_bundle`, `materialize`, and `remove`.

## Invariants

- **Nothing is written before everything is verified.** A failed verify leaves
  the destination byte-identical to its prior state — no partial tree.
- **Fail closed.** Any exception during verification denies.
- **The dev issuer is trusted at personal tier only**, enforced in the verifier
  rather than in the CLI, so a dev-signed bundle cannot verify on an enterprise
  or federal box no matter how it is invoked.
- **Module runtime is never agent-writable.** Materialized files land mode `0444`
  inside `0555` directories, at the deployment root outside the tool fence.

## Install

```bash
pip install arcbundle        # standalone — builds/verifies on a low-side box
# or
pip install arcmas           # full Arc stack
```

Its only runtime dependencies are `arctrust` and Pydantic, so it installs and
runs with no agent stack present.

## Use

```python
from pathlib import Path
import arcbundle

# Low side: package a module folder into a signed bundle directory.
arcbundle.build_bundle(
    Path("modules/browser"),
    module="browser", version="1.2.0",
    private_key=seed, issuer="did:arc:acme",
    out=Path("browser.arcbundle"),
)

# Install host: verify in full, then write atomically. Nothing lands unverified.
verified = arcbundle.verify_bundle(
    Path("browser.arcbundle"),
    tier="federal",
    trusted_issuers={"did:arc:acme": public_key},
)
module_dir = arcbundle.materialize(verified, dest_root=Path("~/.arc/modules"))

# Per agent: copy just the tools + skills the agent may load.
arcbundle.copy_capabilities(module_dir, agent_dir, module="browser")
```

## Tests

`packages/arcbundle/tests/`
