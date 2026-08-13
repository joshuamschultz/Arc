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

### T-972 — the module-capability trust class

**A third trust class, not a third scan root.** `module:*` is now `RootTrust.VERIFIED`:
it runs the signature floor + TOFU gate exactly like an agent-writable root, and skips
the AST import allowlist and the ArcRun-isolated proxy. Those two jobs — *prove who
wrote these bytes* and *contain what they may do* — were fused in one `is_untrusted_root`
boolean, and a module needs the first without the second. Containment exists for code
the model wrote; applying it to module code is what made 17 of 18 modules register zero
tools. `TRUSTED` is now a closed set (`builtins`, `builtins-skills`) and an unclassified
root falls to `UNTRUSTED`, so forgetting to classify one costs it privilege.

**The loader gates every `.py` at a module root, not just `capabilities.py`.** Found by
running the change: `_scan_root` imports each top-level `.py` looking for decorated
values, and an import runs the file. Signing only the declared capability surface would
have verified the door and not the wall — and left `_runtime.py`, `config.py`, and
`store.py` permanently gated, which blocks every `reload()` commit (the transactional
reload refuses while any error is present). `build_bundle` signs every `.py` and every
`SKILL.md` in the payload.

**Pin names had to be qualified by module.** Every module ships `capabilities.py`, so
the loader's bare-stem pin name gave all 18 modules ONE TOFU pin: approving the second
module supersedes the first's hash, and the loader then reads the first as *drifted* —
a hard DENY for no reason but the name. `pin_name_for_path` moved into the loader (the
one place both the gate and `arc trust approve` reach) and returns the artifact's path
within the deployment module root: `scheduler/capabilities`.

**Install grants the trust; it is not a follow-up the operator has to know about.**
`arc module install` pins the key the MANIFEST verified under (`VerifiedBundle.issuer_key`
— never a name looked up a second time) and approves each artifact that verifies beneath
it. Without it a module materializes, enables, and is then refused at load. Both pins land
in `[security.validators]`, which is what `arc trust list` reads, so nothing is trusted
invisibly.

**Re-signing had to defeat the deployment root's own hardening.** `materialize` writes
`0444` inside `0555` so an in-place edit fails loudly. Re-signing is the operator action
that legitimises such an edit, so `artifact_signing._write` borrows write permission for
the one write and hands it back — the same restore-then-modify sequence `arcbundle.remove`
already uses. Asserted both ways: the sidecar is rewritten, and the modes are unchanged
afterwards.

**Regression found while testing:** `arc trust approve` reported "Still gated" on every
successful approval. It re-looked-up the item by NAME, but a refused candidate is reported
under its file/folder name and a loaded one under its metadata name — so a success is
precisely when the name changes. Now matched by path.

**`arc trust approve` could only sign what was already GATED** (raised by Josh). It resolved
its target from `list_gated()` without `include_loaded=True`, which made the PRD's headline
workflow impossible to perform: on a personal-tier box `auto_run_agent_code` lets a
hand-written skill load, so it is never gated — and so could never be signed on the laptop it
was written on. Same restriction blocked pre-signing before shipping to a stricter tier, and
re-signing an artifact that loads today but was edited a minute ago. Approve now resolves from
the full inventory; `list` keeps its gated-only default. Signing is a statement about bytes,
not a repair for a refusal. Ambiguity (one name, two artifacts at different scan roots) is now
an explicit error rather than a silent first-match, and the output distinguishes a first
signature from a re-signature.

**Two "install" fictions in the test suite.** `test_workpad_e2e` and
`test_extension_conformance` `copytree`d a module into the deployment root. That was
indistinguishable from an install while module roots were trusted; it is not now, since
a copied tree carries no sidecars. Both build, verify, and materialize a real signed
bundle. Likewise the module-registration unit tests used bare root names (`("tasks", …)`)
that no production path produces — now `module:tasks`, which is what `agent_lifecycle`
builds.

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

- **`arc skill create` writes an UNSIGNED `SKILL.md`** (`arccli/commands/skill.py:233-234`),
  so a CLI-scaffolded skill does not load above personal tier. Deliberately left unsigned
  rather than wired up: what that command writes is a stub whose very next instruction is
  "edit this", and a signature over a stub is invalidated by that edit — leaving a DRIFTED
  sidecar, which the loader treats as tamper (a hard DENY) rather than as merely unsigned
  (NEW_SIGHTING). The command also has no `--agent`, so it could pin neither the key nor the
  hash that a signature needs to be useful. Signing belongs after the content is real; the
  command now says so as step 3 of its next-steps output, which works because approve is no
  longer gated-only. Revisit only if `arc skill create` gains agent context.

- **`arc module remove` does not withdraw the trust `arc module install` grants.**
  Install pins the issuer's key and approves each artifact's hash in the agent's
  `arcagent.toml`; remove deletes the runtime, the per-agent copy, and the `[modules.NAME]`
  entry, and leaves both pins behind. Stale approvals are inert (the artifact is gone), but
  the trusted KEY is a live trust anchor for a module nobody installed. The inverse is not
  a one-liner: `capability_signing.revoke`'s `_key_still_in_use` scans the AGENT root, while
  module artifacts live at the deployment root, so it would unpin an issuer whose other
  modules are still installed. Needs a module-aware `untrust_bundled_capabilities` that
  checks the whole module root before unpinning. Deliberately not done in T-972.

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
