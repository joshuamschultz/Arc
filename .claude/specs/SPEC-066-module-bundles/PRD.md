# Product Requirements Document: Module Bundles and Capability Signing

## Context References

- **Personas:** [.claude/steering/product.md#user-personas](../../steering/product.md#user-personas)
- **Constraints:** [.claude/steering/product.md#business-constraints](../../steering/product.md#business-constraints)
- **Metrics Framework:** [.claude/steering/product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework)
- **Current Phase:** [.claude/steering/roadmap.md#current-phase](../../steering/roadmap.md#current-phase)

## Product Overview

### Vision
A deployment contains exactly the agent capabilities it was approved for — nothing more sitting on disk unused — and every capability that loads, including one an agent improved for itself, carries a signature an operator produced through a documented command.

### Problem Statement
Two defects, one root. First, `pip install arc-agent` writes all eighteen modules regardless of tier, so a federal enclave that must not run the browser module still has its source on disk; extras gate the dependencies but never the code, and no packaging flag installs a subset of a wheel's files. Second, `capability_loader.py:_passes_trust_gate` denies any unsigned capability above personal tier, but nothing in the product writes a detached signature for a capability or skill — `arc trust approve` pins a source hash only, and prints a note conceding the pin changes nothing at personal tier. The result is that an agent-improved skill can never load above personal tier and no command exists that would make it load, while `README.md` already advertises signed agent-authored capabilities. Fixing the first without the second would ship per-agent skill drift with no way to authorize it.

### Value Proposition
A federal operator can point at a directory listing as evidence that an unapproved capability is not present, rather than at a config flag that could be flipped. An agent's skills can improve for that agent on a laptop and be promoted to a hardened box through one reviewed, signed, audited action. The core artifact stops varying by deployment, so it is approved once and the auditable delta becomes a short list of module names rather than a diff of two binaries.

## Personas

See `.claude/steering/product.md#user-personas`. Primary: Federal/Regulated Security Architect (needs absence provable, not configured). Secondary: Agent Developer (Internal) (must keep a fast inner loop once modules leave the wheel). Tertiary: Auditor / Reviewer (reconstructs what was installed, by whom, under which signature).

## User Stories

- **US-1**: As a federal security architect, I want modules I have not approved to be absent from the deployment entirely, so that a capability scan finds no trace of them and absence does not depend on a setting anyone can change..
- **US-2**: As an operator, I want one command set that installs and removes modules identically online and air-gapped, so that adding a capability months later does not mean rebuilding or re-accrediting the product..
- **US-3**: As an operator, I want to sign a new or improved tool or skill from either the CLI or the UI, following a written procedure, so that an agent's capabilities can load above personal tier and I can see what I am approving before I approve it..
- **US-4**: As an agent developer, I want to edit a module and see the change without a release cycle, so that moving modules out of the wheel does not destroy the inner loop and push people to bypass the boundary..
- **US-5**: As an auditor, I want every install, removal, signature, and refusal recorded as an event, so that I can reconstruct what was on the box, who authorized it, and what was turned away..

## Functional Requirements

- **REQ-319** (story US-3, Must): WHEN an operator runs `arc trust approve <name>` THEN the system SHALL write a detached Ed25519 signature over the capability's source artifact, pin the operator public key as that agent's trusted key, and record the TOFU pin, as a single atomic command.
- **REQ-320** (story US-3, Must): WHEN an operator runs `arc trust disapprove <name>` THEN the system SHALL remove the detached signature and the TOFU pin, restoring the capability to its gated state.
- **REQ-321** (story US-3, Must): The system SHALL accept capability approval only from an operator-authenticated caller, and SHALL NOT register approval as a tool on any registry nor accept it over the chat socket.
- **REQ-322** (story US-3, Must): WHERE arcui renders the capability inventory, the system SHALL offer an approve action on every gated row that invokes the same code path as `arc trust approve` and displays the artifact source before approval.
- **REQ-323** (story US-5, Must): WHEN a capability signature is written, revoked, or refused THEN the system SHALL emit an audit event carrying the operator DID, the artifact path, and the source hash.
- **REQ-324** (story US-3, Must): The system SHALL document the signing procedure in `docs/runbooks/signing-capabilities.md`, `docs/walkthrough/10-security-model.md`, and `README.md`, and those documents SHALL ship in the same change as the commands they describe.
- **REQ-325** (story US-2, Must): The system SHALL define a module bundle as a manifest of module name, version, per-file SHA-256, and issuer, carrying a detached Ed25519 signature over the manifest's canonical-JSON encoding.
- **REQ-326** (story US-2, Must): WHEN `arc module install` processes a bundle THEN the system SHALL verify the manifest signature and every declared file hash before writing any byte to disk.
- **REQ-327** (story US-2, Must): IF bundle verification fails THEN the system SHALL write nothing, leave no partial tree, and exit non-zero.
- **REQ-328** (story US-2, Must): WHEN an operator runs `arc module bundle <names...> -o <file>` THEN the system SHALL emit one signed bundle containing exactly the named modules and their declared dependencies.
- **REQ-329** (story US-2, Must): WHERE no network is available, the system SHALL install modules from a pre-staged bundle file via `arc module install --from <file>` using the same verification path as a networked install.
- **REQ-330** (story US-2, Must): WHEN `arc module install --all` runs THEN the system SHALL install every module the deployment's signed manifest permits and skip those it forbids, rather than failing the command.
- **REQ-331** (story US-4, Should): WHERE the deployment tier is personal, the system SHALL support `arc module install --from-source <name>`, which bundles the module from the repository source catalog, signs it with a development key, and installs it through the same verify-then-materialize path used by a released bundle.
- **REQ-332** (story US-5, Must): WHEN a bundle is verified, refused for an invalid signature, or refused for a content-hash mismatch THEN the system SHALL emit the corresponding audit event naming the bundle, the module, and the issuer.
- **REQ-333** (story US-1, Must): The system SHALL resolve the module scan root to `${ARC_CONFIG_DIR:-~/.arc}/modules/` and SHALL NOT scan the installed package directory for modules.
- **REQ-334** (story US-1, Must): The system SHALL load every module runtime by filesystem path rather than by import name, using one loader for every module regardless of origin.
- **REQ-335** (story US-1, Must): The system SHALL write materialized module runtime files mode `0444` inside directories mode `0555`, located outside the agent tool fence, writable only by the operator-run install and remove commands.
- **REQ-336** (story US-1, Must): The `arc-agent` distribution SHALL contain no module code and SHALL be byte-identical across personal, enterprise, and federal deployments.
- **REQ-337** (story US-3, Must): WHEN a module is installed for an agent THEN the system SHALL copy that module's tools and skills into `<agent_dir>/capabilities/<name>/`, leave its runtime at the deployment root, and subject the copies to the existing untrusted-root trust gate.
- **REQ-338** (story US-2, Must): WHEN `arc module remove <name>` runs THEN the system SHALL delete the materialized runtime, delete the copied capabilities, drop the config entry, and exit non-zero with an explicit error if any of those does not complete.
- **REQ-339** (story US-1, Must): WHILE any subset of modules is absent, including all of them, the system SHALL complete an agent turn without error.

## MoSCoW Priorities

| Priority | Requirements |
|---|---|
| Must | REQ-319, REQ-320, REQ-321, REQ-322, REQ-323, REQ-324, REQ-325, REQ-326, REQ-327, REQ-328, REQ-329, REQ-330, REQ-332, REQ-333, REQ-334, REQ-335, REQ-336, REQ-337, REQ-338, REQ-339 |
| Should | REQ-331 |
| Could | _(none)_ |
| Won't | _(none)_ |

## Success Metrics

Framework: `.claude/steering/product.md#success-metrics-framework` (compliance and quality variants). Targets: a federal deployment contains zero source files for any module absent from its signed manifest, verified by filesystem scan; the absent-safe suite passes for every one of the eighteen modules removed individually; zero load paths exist that register a capability without a verified signature above personal tier; `mkdocs --strict` builds with the new runbook linked; `ruff check` and `mypy --strict` clean; arcagent core stays under the 3,500 LOC budget (ADR-004) with bundle verification and install logic housed outside `core/`.

## Risks and Constraints

Executable code placement — module runtime lands outside the wheel for the first time, and a directory an agent could write to would be a self-modification path (ASI05/ASI06); mitigated by placing runtime at the deployment root outside the tool fence, mode 0444/0555, with only operator-run commands able to write. Scan-root growth — loading capabilities in place would add a root per module and per extension, an unbounded threat surface; mitigated by copying into one fixed per-agent directory so the loader's root set never grows. Development key becoming a production path — `--from-source` signs with a non-release key; mitigated by trusting that key at personal tier only, so a dev-signed module cannot load on an enterprise or federal box. Migration breakage — moving eighteen modules out of the wheel can silently break anything that assumed a module's contribution was guaranteed rather than nullable; mitigated by landing the absent-safe suite and the loader changes before the first module moves. Rubber-stamped approval — a UI approve button invites clicking without reading; mitigated by requiring the artifact source to be displayed on the row before the action is available.

## Open Questions

_(none)_
