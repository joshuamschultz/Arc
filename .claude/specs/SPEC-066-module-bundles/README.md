# Specification: Spec 066 Module Bundles

**Feature:** `SPEC-066-module-bundles`
**Created:** 2026-08-11

## Status

| Doc | Status | Last Update |
|---|---|---|
| PRD | approved | 2026-08-11 |
| SDD | approved | 2026-08-11 |
| PLAN | draft | 2026-08-11 |
## Steering References

- Product: [`../../steering/product.md`](../../steering/product.md)
- Tech: [`../../steering/tech.md`](../../steering/tech.md)
- Structure: [`../../steering/structure.md`](../../steering/structure.md)
- Roadmap: [`../../steering/roadmap.md`](../../steering/roadmap.md)

## Decision Log Snippets

Cross-feature decisions referenced from [`../../decisions-log.md`](../../decisions-log.md):

_(none yet — link decisions as `[D-NNN](../../decisions-log.md#d-nnn)` when they apply to this feature)_

## Phase Notes

### Phase 1: Foundation (T-949..T-955)

**The pinned trust key had to become a SET.** REQ-319 says approval pins the operator
key as "that agent's trusted key", but the loader pinned exactly one key — the agent's
own DID key, which is what makes SPEC-033 self-signed skills load. Replacing it would
have silently broken them. The set lives inside the loader's gate, looping over the
existing singular `TrustBackend` contract, so arctrust/arcskill/arcprompt/arcteam were
untouched. The alternative (renaming `trusted_public_key` everywhere) was 61 call sites
across six packages, nearly all unrelated to capability loading.

**Regression caught in review:** the signing rework made `arc trust disapprove` refuse
whenever the artifact was already deleted. Before this spec that was cosmetic; now a pin
carries a trusted verify key, so refusing would strand a trust anchor in the config that
no command could remove. Restored with a regression test.

**Test-quality note:** seven of the eight original T-949 tests handed the loader the
correct key directly, so a `sign()` that wrote a key nothing ever read would have passed
them all. Added `test_pinned_key_is_consumed_by_the_real_load_posture`, which drives the
live inventory seam and never supplies the key. That is the test that actually proves the
wiring — the repo's known "correct predicate, dead activating wiring" failure mode.

### Phase 2: Core (T-956..T-962)

**Two findings beyond the spec.** The verifier carries the verified payload in memory and
materializes from there, closing a TOCTOU window between "these bytes hashed correctly"
and "these bytes were written" that the SDD did not specify. It also refuses payload
files the manifest never declared — undeclared code riding along with verified code —
and treats a symlink as undeclared whatever it points at.

**Canonical JSON delegates to `arctrust.canonical_json`** rather than re-implementing.
The parity test pivots both encoders on that shared primitive, so neither can drift.

### Phase 3: Integration (in progress)

**The spec's headline security control was not actually in place.** The PRD's mitigation
for putting module code outside the wheel was "runtime at the deployment root outside the
tool fence, mode 0444/0555, with only operator-run commands able to write." Two of three
claims were false:

- `0444/0555` is not a control against the agent. It runs as the same OS user that
  installed the modules, so `bash` can `chmod u+w` and write.
- "Outside the tool fence" was not structurally true. Personal-tier `bash` is host bash —
  the file-path fence never sees the path. And an `allowed_paths` entry over the Arc home
  (a plausible operator config) hands write access to every module runtime on the box.

Enterprise and federal were safe only incidentally, via arcrun's workspace-only bind
mount. T-963 exists because this would otherwise have shipped as "mitigated".

**Good news:** all 17 discovered modules already pass the absent-safe suite, which
materially de-risks T-972.

## Decisions taken during implementation

- **D-066-1 — Trusted capability keys are a set, not a single key.** Persisted as
  `[security.validators] trusted_keys`. Operator-signed and agent-self-signed capabilities
  must both be able to pass; neither source may evict the other.
- **D-066-2 — Capability signing goes through `arctrust.Signer`, not a raw seed.**
  Federal forces `custody = vault_transit` and rejects `in_process`, so a seed-only
  signing path cannot run on a federal box at all — the tier this spec targets. Per Josh:
  use the same model as the rest of Arc's key custody — personal holds the key locally,
  enterprise and above hold it in a vault. `arctrust.signer.verify_signature` already
  supports Ed25519 and ECDSA-P256, and `ArtifactSignature.algorithm` already exists, so
  `verify_artifact` dispatching on algorithm is the missing piece rather than new crypto.
- **D-066-3 — One wheel, not two.** Confirmed against SDD alternative D-635, which
  rejected a per-deployment wheel because federal would then run a binary never built or
  tested elsewhere. REQ-336 requires one byte-identical distribution containing no module
  code; modules arrive as signed bundles at every tier.

## Learnings

Feature-specific insights captured here. Global / reusable patterns go to memory via `/memorize`.

_(none yet)_

## Open Questions

_(none yet)_
