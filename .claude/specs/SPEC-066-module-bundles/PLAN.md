# Implementation Plan: Module Bundles and Capability Signing

## Context References

- **PRD:** [PRD.md](./PRD.md)
- **SDD:** [SDD.md](./SDD.md)
- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Roadmap:** [.claude/steering/roadmap.md](../../steering/roadmap.md)

## Phase 1: Foundation

- [ ] **T-949**: (red) Failing test: a signed capability passes the loader's own trust gate; an unsigned one is denied above personal
  - domain: test
  - Components: COMP-010
  - Requirements: REQ-319
  - Acceptance: Test drives the real `capability_loader._passes_trust_gate` with `require_signature=True` and a pinned key. Red for the right reason: no signing function exists yet. Asserts denial detail is `unsigned`, not a generic error.
- [ ] **T-950**: (green) Implement capability signing and revocation against the operator key
  - domain: auth
  - Components: COMP-010
  - Requirements: REQ-319, REQ-320
  - Acceptance: `sign(artifact, signer, config_path)` writes a detached Ed25519 signature, pins the operator public key as the agent's trusted key, and records the TOFU pin. `revoke()` removes all three. T-949 goes green with no change to the loader.
- [ ] **T-951**: (green) Rework `arc trust approve` / `disapprove` to sign, not pin a hash alone
  - domain: backend
  - Components: COMP-011
  - Requirements: REQ-319, REQ-320, REQ-321
  - Acceptance: One command signs + pins key + pins hash. The hash-only path and the note conceding it changes nothing at personal tier are deleted, not kept beside. A non-operator caller is refused. Post-approval verdict is re-read through the inventory seam and reports `loaded`.
- [ ] **T-952**: (green) `POST /api/trust/approve` signs through the same function
  - domain: api
  - Components: COMP-012
  - Requirements: REQ-321, REQ-322
  - Acceptance: Route calls COMP-010 instead of `_approve_pin`. Route table, operator-role gate, roster resolution, audit emission, and the lazy inventory-seam import are untouched. Architecture test still proves arcui imports arcagent only via the seam.
- [ ] **T-953**: (green) Capability trust panel: render artifact source, then allow approve
  - domain: ui
  - Components: COMP-013
  - Requirements: REQ-322
  - Acceptance: Gated rows render the artifact source text. The approve action is disabled until the source has been displayed. Rebuild the arcui static bundle and restart, since the server caches `static/index.html`.
- [ ] **T-954**: (green) Audit events for capability sign, revoke, and refusal
  - domain: infra
  - Components: COMP-014
  - Requirements: REQ-323
  - Acceptance: `capability.signed` and `capability.signature_revoked` emit through the single `arctrust.audit.emit` point carrying operator DID, artifact path, and source hash. Existing `capability:deny` refusals keep their shape. Asserted on both the CLI and the arcui path.
- [ ] **T-955**: (green) Signing runbook, security-model section, and README correction
  - domain: infra
  - Components: COMP-016
  - Requirements: REQ-324
  - Acceptance: `docs/runbooks/signing-capabilities.md` covers both surfaces and how to read a denial. `docs/walkthrough/10-security-model.md` explains the signature floor running before the TOFU layer. `README.md` supply-chain row describes what the commands actually do. `mkdocs --strict` builds with the runbook in the nav.

## Phase 2: Core

- [ ] **T-956**: (red) Failing test: manifest canonical bytes are stable and match the arcrun verifier convention
  - domain: test
  - Components: COMP-001
  - Requirements: REQ-325
  - Acceptance: Encoding is byte-identical across key orderings and across process runs. A parity case asserts the same bytes as `arcrun/backends/_verifier.py:canonical_json_payload` for an equivalent payload, so the two encoders cannot drift.
- [ ] **T-957**: (green) `BundleManifest` model and canonical-JSON encoding
  - domain: backend
  - Components: COMP-001
  - Requirements: REQ-325
  - Acceptance: Pydantic model with format_version, module, version, issuer, and files of relative path + sha256. Relative paths reject traversal. `canonical_bytes()` turns T-956 green.
- [ ] **T-958**: (green) Bundle signer over canonical manifest bytes
  - domain: auth
  - Components: COMP-002
  - Requirements: REQ-325, REQ-331
  - Acceptance: Produces a detached Ed25519 signature and stamps the issuer. Accepts either a raw key or an `arctrust.Signer`. Never writes payload files.
- [ ] **T-959**: (red) Failing tests: verification refuses a bad signature, a hash mismatch, and leaves no partial tree
  - domain: test
  - Components: COMP-003, COMP-004
  - Requirements: REQ-326, REQ-327
  - Acceptance: Three cases: tampered manifest, tampered payload file, and a write interrupted mid-materialize. Each asserts the destination is byte-identical to its pre-call state and the exit is non-zero. A fourth asserts a dev-signed bundle fails verification at federal tier.
- [ ] **T-960**: (green) Fail-closed bundle verifier
  - domain: auth
  - Components: COMP-003
  - Requirements: REQ-326, REQ-327, REQ-329, REQ-331
  - Acceptance: Verifies the manifest signature against the tier's trusted issuer set, then every declared file hash, before any write. Any exception denies. The dev issuer is trusted at personal tier only, enforced here rather than in the CLI.
- [ ] **T-961**: (green) Atomic materializer with read-only mode bits and inverse remove
  - domain: backend
  - Components: COMP-004
  - Requirements: REQ-327, REQ-335, REQ-336, REQ-338
  - Acceptance: Stage to a temp sibling, fsync, rename. Files land 0444 inside directories 0555. `remove(name)` deletes the tree and raises if it does not complete. T-959 goes green.
- [ ] **T-962**: (green) Bundle builder over the repository source catalog
  - domain: backend
  - Components: COMP-005
  - Requirements: REQ-328
  - Acceptance: Packages one module folder into a `.arcbundle` of manifest, signature, and payload tree. Round-trips through the verifier. Runs offline with no network access.

## Phase 3: Integration

- [ ] **T-963**: (red) Security test: agent tools cannot reach the deployment module root
  - domain: test
  - Components: COMP-004
  - Requirements: REQ-335
  - Acceptance: Drives the real `write`, `edit`, and `bash` tools against paths under `${ARC_CONFIG_DIR}/modules/` and asserts each is refused by the tool fence, not merely by a mode bit. This is the ASI05/ASI06 control and does not ride along on the absent-safe suite.
- [ ] **T-964**: (green) Move the module scan root to the deployment directory
  - domain: backend
  - Components: COMP-006
  - Requirements: REQ-333, REQ-336
  - Acceptance: `_MODULES_DIR` resolves to `${ARC_CONFIG_DIR:-~/.arc}/modules/` through the existing config-dir resolution. The installed package directory is no longer scanned. Folder-presence discovery, `ModuleStatus`, and `active_modules()` semantics are unchanged and their tests still pass untouched.
- [ ] **T-965**: (green) Load module runtimes by path instead of by import name
  - domain: backend
  - Components: COMP-007
  - Requirements: REQ-334
  - Acceptance: `configure_module_runtimes` uses `spec_from_file_location` against the resolved folder, one loader for every module. The `configure()` signature-introspection kwarg dispatch is unchanged. Nothing is added to `sys.path`.
- [ ] **T-966**: (red) Absent-safe suite parametrized over live discovery
  - domain: test
  - Components: COMP-015
  - Requirements: REQ-339
  - Acceptance: For each name from `discover_modules()`: remove that module's tree, start a real agent, run a turn end to end, assert success. Plus the all-absent case. Parametrization derives from discovery so a module added later is covered without editing the test. Must be green before any module leaves the wheel.
- [ ] **T-967**: (green) `arc module` command set
  - domain: backend
  - Components: COMP-008
  - Requirements: REQ-328, REQ-329, REQ-330, REQ-338
  - Acceptance: `list`, `install <names...>`, `install --all`, `install --from <bundle>`, `remove <name>`, `bundle <names...> -o <file>`. Install writes the config entry so install and enable are one step. `--all` installs every module the signed deployment manifest permits and skips the rest without failing. Every subcommand is exercised in its bare form, not only with flags.
- [ ] **T-968**: (green) Per-agent copy of module tools and skills, and its inverse
  - domain: backend
  - Components: COMP-009
  - Requirements: REQ-337, REQ-338
  - Acceptance: Install copies tools and skills to `<agent_dir>/capabilities/<name>/`; runtime is never copied. Copies land in the existing `agent` root and are adjudicated by `_UNTRUSTED_ROOTS` unchanged, loading clean while Arc-signed. Remove deletes runtime, copies, and config entry, exiting non-zero with an explicit error if any part does not complete.
- [ ] **T-969**: (green) Audit events for bundle verify, refuse, install, and remove
  - domain: infra
  - Components: COMP-014
  - Requirements: REQ-332
  - Acceptance: `module.bundle.verified`, `module.signature_invalid`, `module.content_hash_mismatch`, `module.installed`, `module.removed`, each naming bundle, module, and issuer. One emission point per outcome; sinks fan out unchanged.

## Phase 4: Polish

- [ ] **T-970**: (green) Exclude modules from the wheel; release CI builds and signs bundles
  - domain: infra
  - Components: COMP-004, COMP-006
  - Requirements: REQ-336
  - Acceptance: Hatchling excludes `arcagent/modules/` from the built wheel. Release CI packages each module folder into a signed bundle and publishes them as release assets. A test asserts the built wheel contains no module source. The wheel is byte-identical regardless of target tier.
- [ ] **T-971**: (green) `arc module install --from-source` for development, signed with a dev key
  - domain: infra
  - Components: COMP-002, COMP-005, COMP-008
  - Requirements: REQ-331
  - Acceptance: Bundles one module from the source catalog, signs with a locally generated development key, installs through the identical verify-then-materialize path. Nothing is symlinked and no path skips verification. A dev-signed bundle is refused at enterprise and federal, asserted by T-959's tier case.
- [ ] **T-972**: (refactor) Migrate all eighteen modules out of the wheel, one at a time
  - domain: mixed
  - Components: COMP-006, COMP-009, COMP-015
  - Requirements: REQ-336, REQ-337, REQ-339
  - Acceptance: Order by risk: `memory` first (its `NullBrain` default already makes absent-safe true and it proves the machinery), then `browser` (the case that motivated the work), then the remaining sixteen. The absent-safe suite runs green after each individual move, not only at the end. Any module that fails is fixed before the next one starts.
- [ ] **T-973**: (refactor) Module distribution docs and package index
  - domain: infra
  - Components: COMP-016
  - Requirements: REQ-324
  - Acceptance: `docs/building/modules.md` covers authoring against the bundle format and the three filesystem locations. A new air-gapped staging runbook covers `arc module bundle` on the low side and `install --from` inside. `docs/building/package-index.md` gains `arcbundle`. `mkdocs --strict` passes.

## Traceability

| Requirement | Tasks |
|---|---|
| REQ-319 | T-949, T-950, T-951 |
| REQ-320 | T-950, T-951 |
| REQ-321 | T-951, T-952 |
| REQ-322 | T-952, T-953 |
| REQ-323 | T-954 |
| REQ-324 | T-955, T-973 |
| REQ-325 | T-956, T-957, T-958 |
| REQ-326 | T-959, T-960 |
| REQ-327 | T-959, T-960, T-961 |
| REQ-328 | T-962, T-967 |
| REQ-329 | T-960, T-967 |
| REQ-330 | T-967 |
| REQ-331 | T-958, T-960, T-971 |
| REQ-332 | T-969 |
| REQ-333 | T-964 |
| REQ-334 | T-965 |
| REQ-335 | T-961, T-963 |
| REQ-336 | T-961, T-964, T-970, T-972 |
| REQ-337 | T-968, T-972 |
| REQ-338 | T-961, T-967, T-968 |
| REQ-339 | T-966, T-972 |

## Open Questions

_(none)_
