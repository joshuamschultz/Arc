<div align="center">

# 📦 arcbundle

### **Signed Module Bundles for Arc**
*The distribution unit that makes a module's absence provable — a directory listing, not a config flag.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-002550.svg)](https://opensource.org/licenses/Apache-2.0)
[![Tests](https://img.shields.io/badge/tests-48-0055BC.svg)](#status)
[![Coverage](https://img.shields.io/badge/coverage-84%25-003B82.svg)](#status)
[![Strict mypy](https://img.shields.io/badge/mypy-strict-0073FE.svg)](#status)
[![Ed25519](https://img.shields.io/badge/crypto-Ed25519-F68D2E.svg)](#two-signatures-one-key)
[![Verification](https://img.shields.io/badge/verify-fail--closed-F68D2E.svg)](#invariants)

</div>

---

## ✨ What is arcbundle?

`arcbundle` is the on-disk **distribution unit** for Arc modules. Each module ships as a separately
signed `.arcbundle`, verified in full before a single byte reaches disk, materialized read-only at
the deployment root outside every agent's tool fence, then copied per agent as tools and skills.

It exists because packaging cannot express absence. `pip install arc-agent` used to write every
module to disk regardless of tier, so a federal enclave that must not *run* the browser module still
had its source sitting on the box. Extras gate dependencies, never files, and no packaging flag
installs a subset of a wheel. `arcbundle` makes the wheel carry no module code at all — so the
`arc-agent` wheel is byte-identical across every tier, and what a deployment can run is decided by
which bundles were approved and installed.

The result an operator can point at: **absence is a directory listing, not a config flag someone
could flip.**

---

## ⭐ Top Features

What makes `arcbundle` a supply-chain control rather than an unzip helper:

### **Supply-Chain Security**
- **Nothing written before everything verified** — a failed verify leaves the destination byte-identical to its prior state; verification never mutates the filesystem
- **Two-signature model** — the manifest signature proves *this bundle is what its issuer built*; a per-file `.arcsig` sidecar proves *this file is what its issuer signed*
- **Verified bytes travel in memory** — `VerifiedBundle.files` carries the exact bytes that were hashed, so nothing is re-read from the bundle directory at write time
- **No undeclared files** — a payload tree carrying anything the manifest never declared is a refusal, not an extra

### **Deployment Control**
- **Module runtime is never agent-writable** — materialized files land mode `0444` inside `0555` directories, at the deployment root outside the tool fence
- **Atomic materialization** — stage → fsync → rename, with the previous tree displaced and restored if the rename fails; a corrupted bundle never leaves partial artifacts
- **Tier pinned in the verifier** — the dev issuer (`did:arc:dev`) is trusted at personal tier only, enforced in `verifier.py` rather than in a CLI flag, so no invocation can widen it
- **Low-side operation** — depends on `arctrust` and Pydantic and nothing else, so bundles build and verify on a staging box with no agent stack installed

### **Audit & Compliance**
- **Emission at the decision point** — `module.bundle.verified`, `module.signature_invalid`, `module.content_hash_mismatch`, `module.installed`, `module.removed`, each emitted from inside this package so every surface that installs a bundle records the same fact
- **Refusals name what they refused** — a signature failure after the manifest parsed still reports module and issuer, which is what an auditor needs
- **Audit never breaks the install** — sink failures are swallowed and logged (NIST AU-5)

### **Fail-Closed Refusals**
- **One exception per failed gate** — `BundleManifestError`, `BundleSignatureError`, `BundleContentHashError`, `BundleMaterializeError`; all mean the same thing operationally: nothing was installed
- **Not rooted in `ValueError`** — Pydantic v2 converts a `ValueError` raised inside a validator into a `ValidationError`; the path and name guards live *in the model*, so their refusal must survive that conversion and reach the caller by its own name
- **Guards where the value is constructed** — `_paths.require_safe_name` / `require_relative_path` run at construction, not at use, so no caller can route around them

---

## 🏗️ Where It Fits

```text
arccli · arcagent                    installers and readers
      |
      v
  arcbundle                          the signed distribution unit
      |
      v
  arctrust                           Ed25519 + canonical JSON
```

`arcbundle` is a **leaf beside `arctrust`**. It imports `arctrust` and Pydantic — nothing else, ever.
It knows nothing of `arcagent`: the agent only ever *reads* an already-materialized directory, so the
nucleus stays ignorant of distribution entirely, and a bundle can be built or verified on a low-side
box that has no agent stack present.

---

## 🚀 Install

```bash
pip install arcbundle         # standalone — builds/verifies on a low-side box
# or
pip install arcmas            # full Arc stack
```

Its only runtime dependencies are `arctrust` and Pydantic, so it installs and runs with no agent
stack present.

---

## 🧪 Quick Example

```python
from pathlib import Path

import arcbundle
import arctrust

issuer_key = arctrust.generate_keypair()    # Ed25519; an arctrust.Signer works too

# 1. Low side: package a module folder into a signed bundle directory.
#    Every .py and every SKILL.md also gets a detached .arcsig sidecar,
#    written INTO the payload so the manifest signature covers it too.
arcbundle.build_bundle(
    Path("modules/browser"),
    module="browser",
    version="1.2.0",
    private_key=issuer_key.private_key,
    issuer="did:arc:acme",
    out=Path("browser.arcbundle"),
)

# 2. Install host: verify in full. Signature → canonical form → every declared
#    file hash → no undeclared files. Any failure raises and writes nothing.
verified = arcbundle.verify_bundle(
    Path("browser.arcbundle"),
    tier="federal",
    trusted_issuers={"did:arc:acme": issuer_key.public_key},
    sink=audit_sink,
    actor_did=operator_did,
)

# 3. Write atomically from the bytes that were verified — never re-read from
#    the bundle dir. Lands 0444 inside 0555 at the deployment module root.
module_dir = arcbundle.materialize(verified, arctrust.paths.module_root())

# 4. Per agent: copy just the tools + skills the agent may load.
#    _runtime.py is deliberately left behind.
arcbundle.copy_capabilities(module_dir, agent_dir, module="browser")
```

---

## 🧩 What's Inside

### Manifest (`arcbundle.manifest`)

| Symbol | What It Does |
|---|---|
| `BundleManifest` | The signed on-disk shape: `format_version`, `module`, `version`, `issuer`, and the sorted `files` list. Validators refuse an unknown format version, a module name that is not one directory component, an empty identifier, and a path declared twice |
| `FileEntry` | One declared payload file — relative path plus lowercase-hex SHA-256. Validators keep the path inside the bundle and the digest well-formed |
| `canonical_bytes()` | The exact bytes a signature binds to |
| `MANIFEST_FORMAT_VERSION` | The signed shape this build reads |

### Signing (`arcbundle.signer`)

| Symbol | What It Does |
|---|---|
| `sign_manifest(manifest, *, private_key, issuer)` | Detached Ed25519 signature over a manifest's canonical bytes. Accepts a raw seed or an `arctrust.Signer`, so vault/HSM custody works unchanged |

### Verification (`arcbundle.verifier`)

| Symbol | What It Does |
|---|---|
| `verify_bundle(root, *, tier, trusted_issuers, sink=None, actor_did=None)` | The full fail-closed gate chain; returns a `VerifiedBundle` or raises |
| `VerifiedBundle` | The only thing `materialize` accepts: the manifest, the verified payload **bytes**, and `issuer_key` |
| `TIERS` · `DEV_ISSUER` · `DEV_ISSUER_TIERS` | `personal` / `enterprise` / `federal`; `did:arc:dev` accepted at personal only |
| `MANIFEST_NAME` · `SIGNATURE_NAME` · `PAYLOAD_DIR` | `manifest.json`, `manifest.sig`, `files/` |

**The gate chain, in order:** read manifest + detached signature → parse into the model (its own path
and name guards fire here) → resolve the issuer key for this tier → verify the signature over the raw
bytes → re-check the raw bytes are the one canonical spelling → hash every declared payload file →
refuse any file the manifest never declared.

**Why `issuer_key` travels on the result:** an installer that must pin this issuer as a trusted
capability-verification key has to pin the key that *actually passed*, not whatever the trust store
answers a second later.

**Why the canonical-form re-check:** the signature covers the bytes on disk. Pinning those bytes to
one canonical spelling stops a re-encoded manifest from meaning one thing to this parser and another
to the next reader of the same signed blob.

### Materialization (`arcbundle.materializer`)

| Symbol | What It Does |
|---|---|
| `materialize(verified, dest_root, *, sink=None, actor_did=None)` | Stage → harden → fsync → rename into `dest_root / <module>`. Writes only bytes carried on `VerifiedBundle`; a failed rename restores the displaced tree before the exception leaves the frame |
| `remove(name, dest_root, *, ...)` | The inverse; raises `BundleMaterializeError` if it does not complete |
| `FILE_MODE` · `DIR_MODE` | `0444` files inside `0555` directories |

### Building (`arcbundle.builder`)

| Symbol | What It Does |
|---|---|
| `build_bundle(source_dir, *, module, version, private_key, issuer, out)` | Walk the module folder (never following symlinks, skipping `__pycache__` / `.DS_Store` / `.pyc` / `.pyo`), sign each adjudicated artifact into a `.arcsig` sidecar, write the payload, then the canonical manifest and its signature. Refuses an `out` that already exists, so a build can never half-overwrite an earlier bundle |

**Every `.py` gets a sidecar, not only `capabilities.py`:** the capability loader imports each `.py`
at a module root looking for decorated values, and an import runs the file. Signing the declared
capability surface while leaving `config.py` beside it unsigned would verify the door and not the
wall. `SKILL.md` too — its text is injected straight into the agent's prompt (LLM01 / ASI06).

### Per-Agent Capability Copy (`arcbundle.capability_copy`)

| Symbol | What It Does |
|---|---|
| `capability_dir(agent_dir, module)` | The one path rule both halves share: `<agent_dir>/capabilities/modules/<module>` |
| `copy_capabilities(module_dir, agent_dir, *, module)` | Copy the module's `capabilities.py` and `skills/` into the agent's capability root, normalizing modes to `0644`/`0755` on the way |
| `remove_capabilities(agent_dir, *, module)` | The inverse; `False` when there was nothing to remove |
| `CAPABILITIES_DIR` · `CAPABILITY_FILE` · `SKILLS_DIR` · `SIGNATURE_SIDECAR_SUFFIX` | The on-disk names the copy is defined in terms of |

**Why modes are normalized:** the deployment tree is hardened to `0444` inside `0555`, correct for a
root only the operator writes. A copy that inherited those bits could be neither removed nor signed
in place — and approving a capability means writing a sidecar beside it. The copy's trust comes from
the loader's adjudication of the root it sits in, never from a mode bit.

**Why the `modules/` level is not decoration:** `<agent_dir>/capabilities/skills/` is already a
conventional loader root. Copying a module *named* `skills` straight under `capabilities/` aimed the
copy at that root, its `capabilities.py` landed where only skill folders belong, and the resulting
permanent scan error blocked every reload commit. A namespace of their own beats a reserved-name list
the next convention would outgrow.

### Refusals (`arcbundle.errors`)

| Symbol | What It Means |
|---|---|
| `BundleError` | Base of every refusal — catch this to exit non-zero on any of them |
| `BundleManifestError` | The manifest is malformed, non-canonical, or declares an unsafe path |
| `BundleSignatureError` | The manifest signature is absent, invalid, or from an issuer this tier does not trust |
| `BundleContentHashError` | A declared payload file is missing or altered, or the tree carries an undeclared file |
| `BundleMaterializeError` | An atomic write or an inverse remove did not complete |

### Internals (`arcbundle._paths`, `arcbundle._audit`)

| Symbol | What It Does |
|---|---|
| `require_safe_name` · `require_relative_path` | The write-primitive guards. A manifest string reaching a deployment-root join unguarded is an arbitrary-write primitive (ASI05 / ASI06) |
| `_audit` emitters | One emitter per decided outcome; sinks fan out via `arctrust.audit.emit` |

---

## 🔐 Two Signatures, One Key

The **manifest** signature says *this bundle is what its issuer built*. It is checked once, at
install, and the bundle directory can be deleted afterwards.

A per-file **`.arcsig`** sidecar beside each adjudicated artifact says *this file is what its issuer
signed*. That is the question the capability loader asks on every scan, long after the bundle is
gone: a `module:*` scan root is **verified, not trusted**, so an artifact whose bytes changed since it
was signed stops loading.

`builder` writes those sidecars into the payload, so they are covered by the manifest signature *and*
by the verifier's refusal of undeclared files — a sidecar swapped in transit is a content-hash
mismatch, never a new trust anchor.

---

## 📍 Where a Module Lands

Two destinations, two trust properties, no overlap:

| What | Where | Who may write it |
|---|---|---|
| Runtime (`_runtime.py` and its support) | `arctrust.paths.module_root()` — `<arc_runtime>/modules/<name>/` | The operator install only — `0444` in `0555`, outside the tool fence |
| Capability surface (`capabilities.py`, `skills/`) | `<agent_dir>/capabilities/modules/<name>/` | The agent — which is why the loader adjudicates it as verified and requires a valid signature at every tier |

`copy_capabilities` copies only the second, per agent. `_runtime.py` is never copied to an agent.

---

## 🛡️ Invariants

| Invariant | How |
|---|---|
| **Nothing is written before everything is verified** | A failed verify leaves the destination byte-identical to its prior state — no partial tree, no mutation on any refusal path |
| **What was verified is what is written** | `VerifiedBundle.files` carries the hashed bytes in memory; `materialize` never re-reads the bundle directory |
| **Fail closed** | Any exception during verification denies. `_verify` has one `return`, on its last line |
| **The dev issuer is personal-tier only** | Pinned in the verifier, not the CLI, so a dev-signed bundle cannot verify on an enterprise or federal box no matter how it is invoked |
| **Module runtime is never agent-writable** | `0444` files inside `0555` directories, at the deployment root outside the tool fence |
| **Every executed or injected artifact is signed** | Each `.py` and each `SKILL.md` carries a `.arcsig`, inside the payload the manifest signature covers |

---

## 📜 Audit

`module.bundle.verified`, `module.signature_invalid`, `module.content_hash_mismatch`,
`module.installed`, `module.removed` — each emitted from inside this package at the point the outcome
is decided, so every surface that installs a bundle records the same fact. Pass `sink=` and
`actor_did=` to `verify_bundle`, `materialize`, and `remove`; sinks fan out from
`arctrust.audit.emit` unchanged. Audit failures are swallowed and logged, never allowed to break the
install (NIST AU-5).

---

## 🧪 Status

```bash
uv run --no-sync pytest packages/arcbundle/tests
```

- **Tests:** 48
- **Coverage:** 84%
- **Type check:** `mypy --strict` clean
- **Lint:** `ruff check` clean

---

## 📄 License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
