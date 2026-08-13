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

### Phase 4 / cross-cutting

**Three modules were never optional.** `arcagent/__init__.py:66-68` hard-imports
`arcagent.modules.scheduler` and `arcagent.modules.session`; `connections.py:71` hard-imports
`arcagent.modules.connectors.install`. Remove the module source and `import arcagent` fails
with `ModuleNotFoundError` — not degraded, dead. That violates Non-Negotiable §2 ("completely
removeable ... fully optional").

It was invisible because the wheel always shipped module source, so "absent" only ever meant
"not in the discovery root", never "not importable". The absent-safe suite was measuring a
tree that could not actually be absent, and its own docstring flagged the blind spot. Finding
it required building all 16 workspace wheels and importing into a CLEAN venv. The confusing
`MemoryIsolationError: no agent DID bound` that broke blueprint boot was the visible tip of it.

Resolution: `modules/__init__.py` stays in the wheel as a namespace anchor (`exclude =
["src/arcagent/modules/*/"]` drops the subdirectories, not the package marker), because module
code internally uses dotted `arcagent.modules.<name>` imports — 85 of 151 module files do —
and the runtime loader registers under the dotted name deliberately so a module's tools share
one set of ContextVars. The anchor is not module code, so REQ-336's intent holds. The
dependency inversion in the three load-bearing seams is the actual fix.

**Test pollution root cause, closed.** `test_cli_ext_smoke.py::TestExtInstall::
test_install_single_file` deliberately installed a capability into the developer's REAL
`~/.arc/capabilities/`, with a comment calling best-effort cleanup "acceptable for smoke
tests". Any concurrent test building a real ArcAgent scans that directory, which made
`test_module_absent_safe` fail 19 times in one run and pass in isolation. Isolating it only
became possible once `arc ext install` resolved through `arc_home()`.

**Bootstrap is the deployment risk.** Nothing in `arc init`, `scripts/`, or CI installs
modules — harmless while they shipped in the wheel. After the exclusion, `git pull && uv sync
&& systemctl restart` gives every agent zero modules, and the agent still BOOTS and answers
chat while the scheduler never fires and tasks never dispatch. A silent skip is the worst
failure mode; the current WARNING-and-continue is not acceptable for a config-enabled module.

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

## Follow-up work found during implementation (NOT in SPEC-066)

- **A bundle cannot declare its package dependencies, and `tasks` needs one nobody
  installs.** `modules/tasks/_dispatch_helpers.py:10` does `from arcteam.types import Entity`
  at module scope, but `arcteam` is not a dependency of `arc-agent`. In the workspace it is
  always installed, so this never surfaced. Measured in a wheel-only venv: 17 of 18 modules
  import cleanly from a materialized deployment root; `tasks` fails with
  `ModuleNotFoundError: No module named 'arcteam'`. Because `_RUNTIME_SPECS["tasks"]` leaves
  `optional=False`, that is not a degraded module — `agent_lifecycle` raises
  `RuntimeError: Required module 'tasks' configuration failed` and the **agent does not
  start**. `BundleManifest` has no dependency field, so `arc module install tasks` verifies
  and materializes happily and the breakage appears only at boot. Two things are needed: a
  dependency declaration in the manifest (or an explicit "modules may only import the
  arc-agent closure" rule enforced by a test), and a decision on whether a module that cannot
  load should ever be able to prevent boot.

- **`arccli/commands/blueprint.py:331` has the same vault-custody defect capability
  signing had.** It carries a "DC-4 known limitation: write_signature needs the raw seed;
  a vault_transit..." comment. Once D-066-2 lands the signer-based artifact path, blueprint
  signing is fixable the same way via the signer overload. Deliberately not done here —
  out of SPEC-066's scope, recorded so it is not lost.
- **Flaky concurrency test:** `arcagent/tests/unit/modules/tasks/test_review_fixes.py::
  TestEnsureStoreBuildsOnce::test_concurrent_first_calls_open_store_exactly_once` failed
  once in a ~5800-test run and passed in isolation. Classic "concurrency test that does not
  force interleaving": an instant mock makes `gather` run sequentially, so it passes for the
  wrong reason and fails only when real scheduling intervenes. Needs a Barrier/Event.
- **Audit the remaining hardcoded `~/.arc` paths repo-wide.** `inventory.py` hardcoded
  `Path("~/.arc/capabilities")` while `arctrust.arc_home()` honors `ARC_CONFIG_DIR`. That
  inconsistency let a test write into a developer's REAL arc home. Fixed in the packages
  touched by this spec; the rest of the repo has not been swept.

## Open Questions

_(none yet)_
