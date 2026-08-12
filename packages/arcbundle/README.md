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
| `builder` | Package a module folder into a signed `.arcbundle` |

## Invariants

- **Nothing is written before everything is verified.** A failed verify leaves
  the destination byte-identical to its prior state — no partial tree.
- **Fail closed.** Any exception during verification denies.
- **The dev issuer is trusted at personal tier only**, enforced in the verifier
  rather than in the CLI, so a dev-signed bundle cannot verify on an enterprise
  or federal box no matter how it is invoked.
- **Module runtime is never agent-writable.** Materialized files land mode `0444`
  inside `0555` directories, at the deployment root outside the tool fence.

## Tests

`packages/arcbundle/tests/`
