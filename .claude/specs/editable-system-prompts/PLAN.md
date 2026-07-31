# Implementation Plan: Editable System Prompts (arcprompt)

## Context References

- **PRD:** [PRD.md](./PRD.md)
- **SDD:** [SDD.md](./SDD.md)
- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Roadmap:** [.claude/steering/roadmap.md](../../steering/roadmap.md)

## Phase 1: Foundation

- **T-736** (red): Scaffold arcprompt as a leaf package with its architecture import guard
  - domain: infra
  - Components: COMP-013
  - Requirements: REQ-121
  - Acceptance: arcprompt builds and installs; tests/architecture/test_no_arcprompt_imports_upward.py fails if arcprompt imports any Arc package other than arctrust; arcprompt added to _CANONICAL_PACKAGES in test_workspace_install.py and to the Makefile install target; make architecture-tests passes.
- **T-737** (red): PromptDocument frontmatter model with derived sha256
  - domain: backend
  - Components: COMP-002
  - Requirements: REQ-127
  - Acceptance: Pydantic model parses name/description/tunable; sha256 derived from content at load; an authored version field is ignored, not honored; malformed frontmatter raises PromptUnparseable; mypy --strict clean.
- **T-738** (red): SignatureVerifier over arctrust with mandatory key pinning
  - domain: auth
  - Components: COMP-003
  - Requirements: REQ-129
  - Acceptance: Delegates to arctrust.verify_artifact; returns True only when digest matches AND signature verifies AND manifest key equals the pinned key; a None pinned key is refused rather than skipped (an unpinned floor is no floor); no tier parameter and no bypass flag exists on the call path.
- **T-739** (red): Decide and lock the signature storage form
  - domain: auth
  - Components: COMP-003, COMP-002
  - Requirements: REQ-129
  - Acceptance: Sidecar vs frontmatter resolved and recorded as a decision. If frontmatter, the signed payload excludes the signature field (git gpgsig pattern) and canonicalization is pinned with tests for CRLF, trailing newline, and YAML key reordering. If sidecar, .arcsig naming matches the existing arc convention and a desync test asserts a missing sidecar fails closed.
- **T-740** (red): PromptCatalog discovery across installed packages
  - domain: backend
  - Components: COMP-005
  - Requirements: REQ-121, REQ-126, REQ-134
  - Acceptance: catalog() enumerates every packaged context/ dir via Path(__file__).parent per the blueprints/loader.py convention; returns PromptRef with package, name, description, stock_path; prompts with no overlay are still listed; reads stock only.
- **T-741** (red): PromptResolver two-layer resolution
  - domain: backend
  - Components: COMP-001
  - Requirements: REQ-122, REQ-124
  - Acceptance: Resolution checks overlay then stock, first match wins; an absent overlay resolves to stock with no warning or error (the normal path); resolver is constructed with overlay root, pinned key, and posture and holds them, with no per-call re-derivation (SPEC-017 precedent).
- **T-762** (red): Fail-loud on invalid overlay and missing stock
  - domain: backend
  - Components: COMP-001, COMP-002, COMP-003
  - Requirements: REQ-125, REQ-126
  - Acceptance: An overlay that is unparseable, empty, or unsigned raises and is never silently replaced by stock; a missing stock prompt raises a packaging error naming package and prompt; tests cover each failure mode independently and assert no fallback path exists.
- **T-742** (red): Decide and assert newline handling for externalized prompts
  - domain: test
  - Components: COMP-001, COMP-009
  - Requirements: REQ-138
  - Acceptance: A documented rule for trailing newlines is chosen and enforced in the loader; a test proves a .md file whose content matches a constant loads byte-identically despite the file's trailing newline; the workpad backslash-continuation case is covered explicitly.
- **T-743** (red): PromptSnapshot: run-start freeze
  - domain: backend
  - Components: COMP-004
  - Requirements: REQ-123
  - Acceptance: snapshot() resolves the full set once and returns an immutable mapping; a test mutating an overlay file mid-run proves every turn still observes the original snapshot; the next run observes the change.
- **T-744** (red): Provenance audit event with resolved values only
  - domain: backend
  - Components: COMP-004
  - Requirements: REQ-131, REQ-132
  - Acceptance: Exactly one audit event per run enumerating package, name, source, sha256 per prompt, plus resolved signer DID for overlays; a test asserts a federal-posture agent's event never records a personal/default value (the SPEC-017 regression); sink payload size verified not to truncate at ~25 prompts.

## Phase 2: Core

- **T-745** (red): Pin byte-identity tests for arcrun prompt constants before moving them
  - domain: test
  - Components: COMP-009
  - Requirements: REQ-138
  - Acceptance: Exact-equality plus sha256 assertions exist for CODE_EXEC_GUIDANCE, CONTAINED_EXEC_GUIDANCE, and every strategy prompt_guidance and description, following the existing test_prompts.py:52 idiom; the weak len(...) > 50 assertion is replaced with exact equality.
- **T-746** (green): Externalize arcrun prompts to context/ and delete the constants
  - domain: backend
  - Components: COMP-009, COMP-001
  - Requirements: REQ-121, REQ-138
  - Acceptance: Every arcrun prompt fragment lives in packages/arcrun/src/arcrun/context/; get_strategy_prompts becomes selection-and-join over loaded documents; T-745's assertions pass, then constants and those temporary assertions are deleted in the same edit; arcrun NCLOC decreases.
- **T-747** (refactor): Delete the dead _DEFAULT_PREFIX duplicate
  - domain: backend
  - Components: COMP-009
  - Requirements: REQ-138
  - Acceptance: _DEFAULT_PREFIX and the unused system_prompt_prefix ctor arg are removed from arcrun/strategies/code.py; the only remaining code-strategy guidance is the externalized prompt; no caller regresses (confirmed: two hits in src/, both its own definition).
- **T-748** (red): Pin byte-identity tests for arcagent prompt constants
  - domain: test
  - Components: COMP-009
  - Requirements: REQ-138
  - Acceptance: Exact-equality plus sha256 assertions for CONTEXT_MAINTAINER_SYSTEM_PROMPT (currently zero coverage), SPAWN_GUIDANCE, _SKILL_USAGE_INSTRUCTION, _SUMMARY_TEMPLATE, and the remaining arcagent prompt constants.
- **T-749** (green): Externalize arcagent prompts to context/ and delete the constants
  - domain: backend
  - Components: COMP-009, COMP-001
  - Requirements: REQ-121, REQ-138
  - Acceptance: arcagent prompts live under packages/arcagent/src/arcagent/context/; T-748's assertions pass; constants and temporary assertions deleted together; capability_registry.format_for_prompt() is left generating XML in code (no slot mechanism), with only its authored prose wrapper externalized.
- **T-750** (green): Externalize arcmemory CONSOLIDATION_SYSTEM_PROMPT
  - domain: backend
  - Components: COMP-009, COMP-001
  - Requirements: REQ-121, REQ-138
  - Acceptance: Byte-identity assertion written first, then the ~4.1 KB prompt moves to packages/arcmemory/src/arcmemory/context/; the constant and its export are deleted; agent_consolidate.py loads through arcprompt.
- **T-751** (green): Externalize the arcskill improver prompt templates
  - domain: backend
  - Components: COMP-009, COMP-001
  - Requirements: REQ-121, REQ-138
  - Acceptance: Static prose from suitegen._prompt, build_reflection_prompt, _build_prompt, build_judge_prompt, and the DIMENSIONS table moves to packages/arcskill/src/arcskill/context/ with named placeholders filled from pre-rendered strings; the dead intent_header parameter in mutate.py is removed; behavior byte-identical for fixed inputs.
- **T-752** (red): Declare packaged markdown in every shipping wheel
  - domain: infra
  - Components: COMP-013
  - Requirements: REQ-140
  - Acceptance: artifacts = ["src/<pkg>/**/*.md"] added to each pyproject.toml that ships prompts, following arcagent/pyproject.toml:136; a test builds a wheel and asserts the context/ markdown is present inside it.

## Phase 3: Integration

- **T-753** (red): Overlay path resolution outside the agent tool root
  - domain: backend
  - Components: COMP-007
  - Requirements: REQ-122, REQ-128, REQ-136
  - Acceptance: Overlay path resolves to <agent_root>/context/<package>/<name>.md; a test proves the path is outside the workspace subtree agent file tools are confined to; deleting the overlay restores stock resolution on the next snapshot.
- **T-754** (red): Security test: agent tools cannot write the overlay tree
  - domain: test
  - Components: COMP-008
  - Requirements: REQ-128
  - Acceptance: Adversarial test attempts direct and traversal-based writes into the overlay tree through the agent's file tools and asserts every attempt is denied and audited; test lives under the package's security/ test dir.
- **T-755** (green): Wire the resolver into agent construction and the assemble_prompt flow
  - domain: backend
  - Components: COMP-006
  - Requirements: REQ-123, REQ-132
  - Acceptance: setup_capabilities constructs the resolver with tier posture, pinned key, and overlay root at construction time; the run-start snapshot feeds agent:assemble_prompt and arcrun's strategy call; an integration test asserts a real agent's assembled prompt is unchanged from pre-migration and the provenance event fires once.
- **T-756** (red): Confirm overlay visibility under the docker backend
  - domain: infra
  - Components: COMP-006, COMP-007
  - Requirements: REQ-123
  - Acceptance: Given arcrun mounts identity.md/context.md read-only in the docker backend, a test or documented finding establishes whether a containerized agent observes overlay writes on the next run without restart; if it does not, the mount is adjusted or the limitation is recorded in the spec README.
- **T-757** (red): SigningAuthority seam resolving the signer from the request principal
  - domain: auth
  - Components: COMP-011
  - Requirements: REQ-130, REQ-131
  - Acceptance: signer_for(request) resolves from the authenticated principal; today returns the deployment operator key for an operator-role caller; no handler references a key directly; the resolved signer DID reaches the audit payload; a test asserts swapping the resolver implementation changes the signer with zero call-site edits.
- **T-758** (red): Prompts API: list and read with diff
  - domain: api
  - Components: COMP-010
  - Requirements: REQ-134, REQ-135
  - Acceptance: GET /prompts lists every prompt across every installed package with stock/overridden state, including prompts with no overlay; GET one returns stock body, effective body, and a server-computed unified diff; stock is read server-side and the client never supplies a path; 200-empty is distinguishable from 503-unreadable per the skill_versions convention; integration tests use real stores per the arcui doctrine.
- **T-763** (red): Prompts API: write and reset with secret scan
  - domain: api
  - Components: COMP-010
  - Requirements: REQ-133, REQ-136
  - Acceptance: PUT writes a signed overlay through the SigningAuthority seam, operator-gated; content matching a known secret pattern returns 400 and audits the refusal with the detected type, reusing _find_secret rather than reimplementing it; DELETE removes the overlay so the prompt resolves to stock on the next run; _confine reused for path safety.
- **T-759** (red): Policy gate on prompt writes
  - domain: auth
  - Components: COMP-014
  - Requirements: REQ-139
  - Acceptance: Overlay writes route through PolicyPipeline as a prompt:write action; no configured rule yields ALLOW (no-op gate); a configured deny rule refuses the write and audits it; arcprompt itself contains no tier branching.

## Phase 4: Polish

- **T-760** (green): Prompts tab, drawer, and the first diff renderer
  - domain: ui
  - Components: COMP-012
  - Requirements: REQ-137
  - Acceptance: Prompts tab lists prompts grouped by package with stock/overridden chips; drawer offers stock | effective | diff toggle plus edit, save, and reset; edit gated on operator mode; server-supplied unified diff rendered (no diff library shipped to the browser); new query hooks and types added; matches the existing drawer and banner conventions.
- **T-761** (refactor): Close quality gates and record residual findings
  - domain: mixed
  - Components: COMP-013, COMP-009
  - Requirements: REQ-121, REQ-140
  - Acceptance: ruff clean, mypy --strict clean, line coverage >= 80% and branch >= 75% on every touched package, arcprompt given a LOC ceiling for consistency, arcrun within its 5,400 budget; zero prompt string literals remain in the four migrated packages; the decomposer.py/_validation.py protected-names inconsistency for context.md is either fixed or raised explicitly.

## Traceability

| Requirement | Tasks |
|---|---|
| REQ-121 | T-736, T-740, T-746, T-749, T-750, T-751, T-761 |
| REQ-122 | T-741, T-753 |
| REQ-123 | T-743, T-755, T-756 |
| REQ-124 | T-741 |
| REQ-125 | T-762 |
| REQ-126 | T-740, T-762 |
| REQ-127 | T-737 |
| REQ-128 | T-753, T-754 |
| REQ-129 | T-738, T-739 |
| REQ-130 | T-757 |
| REQ-131 | T-744, T-757 |
| REQ-132 | T-744, T-755 |
| REQ-133 | T-763 |
| REQ-134 | T-740, T-758 |
| REQ-135 | T-758 |
| REQ-136 | T-753, T-763 |
| REQ-137 | T-760 |
| REQ-138 | T-742, T-745, T-746, T-747, T-748, T-749, T-750, T-751 |
| REQ-139 | T-759 |
| REQ-140 | T-752, T-761 |

## Open Questions

- Signature storage form is resolved by T-739 and blocks T-757 and T-758 — it must be decided before any overlay is written.
- Overlays live under a gitignored path, so overlay change history has no git mechanism; either arcstore-backed history is added or the audit trail is the sole record.
- Whether tunable: false needs enforcement in v1 or stays inert until the v2 optimizer exists.
- Whether identity.md's confirmed absent integrity check is closed in this spec or tracked separately.
- REQ-137 is currently scoped Should — if arcui editing is the primary driver rather than a nice-to-have, it should be promoted to Must before implementation starts.
