# Product Requirements Document: Editable System Prompts (arcprompt)

## Context References

- **Personas:** [.claude/steering/product.md#user-personas](../../steering/product.md#user-personas)
- **Constraints:** [.claude/steering/product.md#business-constraints](../../steering/product.md#business-constraints)
- **Metrics Framework:** [.claude/steering/product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework)
- **Current Phase:** [.claude/steering/roadmap.md#current-phase](../../steering/roadmap.md#current-phase)

## Product Overview

### Vision
Every instruction Arc gives a model is a versioned, signed, inspectable artifact — readable in arcui, tunable per agent without a redeploy, and attributable to exact bytes in the audit trail.

### Problem Statement
Prompts are the only behavior surface in Arc that is invisible. Traces, policy, config, skills, and memory are all inspectable; the ~25 system prompts driving model behavior are triple-quoted string literals across arcrun, arcagent, arcmemory, and arcskill. Changing one means editing Python and redeploying. A new deployment that needs different memory extraction behavior has no seam to vary it without forking, and an auditor cannot see what the fleet is actually instructed to do.

### Value Proposition
Closes the last observability blind spot. A deployment adapts prompts to its domain without forking Arc; an operator retunes fleet behavior in minutes rather than an edit-redeploy cycle; an auditor reads every instruction the fleet runs and verifies its provenance. Establishes the artifact spine that v2 eval-gated and GEPA-style self-tuning work requires.

## Personas

See `.claude/steering/product.md#user-personas`. Primary: Federal/Regulated Security Architect (needs every loaded artifact verified and audited). Secondary: Agent Developer (needs the fast tune-observe loop and safe migration). Tertiary: Auditor / Reviewer (needs to enumerate and diff every prompt without shell access).

## User Stories

- **US-1**: As an operator tuning a deployed fleet, I want to override a specific prompt for one agent and have it take effect without redeploying, so that I can adapt memory extraction and harness guidance to a new deployment in minutes..
- **US-2**: As a deployment owner who does not write Python, I want to adapt prompts to my organization's domain and always see how they differ from what shipped, so that I get domain fit without forking Arc or losing track of my divergence..
- **US-3**: As an auditor, I want to enumerate every prompt an agent runs, read it, diff it against stock, and confirm its signer, so that I can attest to what the system was instructed to do and by whom..
- **US-4**: As an Arc maintainer, I want prompt text externalized out of Python without any behavior change, so that the migration is provably faithful and the packages stay within their budgets and gates..

## Functional Requirements

- **REQ-121** (story US-4, Must): The arcprompt package SHALL load prompt text from markdown files located in a `context/` directory within each owning package, and SHALL NOT import any other Arc package except arctrust.
- **REQ-122** (story US-1, Must): WHEN arcprompt resolves a prompt for an agent THEN the system SHALL check exactly two layers in order — the agent's overlay, then the packaged stock — and SHALL return the first match.
- **REQ-123** (story US-3, Must): WHEN a run begins THEN the system SHALL resolve and snapshot the complete prompt set for that run, and every turn within that run SHALL observe the identical snapshot.
- **REQ-124** (story US-1, Must): WHERE no overlay file exists for a prompt, the system SHALL resolve to packaged stock without warning or error.
- **REQ-125** (story US-4, Must): IF an overlay file is present but its frontmatter is unparseable, its body is empty, or its signature is missing or invalid THEN the system SHALL raise and refuse to load, and SHALL NOT silently fall back to stock.
- **REQ-126** (story US-4, Must): IF a packaged stock prompt is absent at load THEN the system SHALL raise a packaging error naming the missing package and prompt.
- **REQ-127** (story US-2, Must): The system SHALL parse prompt frontmatter through a Pydantic model carrying `name`, `description`, and `tunable`, and SHALL derive version identity as a sha256 digest of the content rather than reading any authored version field.
- **REQ-128** (story US-3, Must): The system SHALL store agent overlays outside the workspace subtree that agent file tools are confined to, such that no agent-invoked tool can address an overlay path.
- **REQ-129** (story US-3, Must): WHEN arcprompt loads an overlay THEN the system SHALL verify its Ed25519 signature against a pinned public key before the text enters any model context, at every deployment tier, with no configuration flag able to bypass verification.
- **REQ-130** (story US-1, Must): WHEN an overlay is written through arcui or the CLI THEN the system SHALL sign it using a signing identity resolved from the authenticated principal of that request, and the write path SHALL NOT reference any specific key directly.
- **REQ-131** (story US-3, Must): The system SHALL record the resolved signer DID in every prompt-write audit event, and SHALL NOT emit a hardcoded or default signer value.
- **REQ-132** (story US-3, Must): WHEN a run begins THEN the system SHALL emit one audit event enumerating every prompt in the run snapshot with its package, name, resolution source, and sha256 digest.
- **REQ-133** (story US-1, Must): IF an overlay write contains content matching a known secret pattern THEN the system SHALL refuse the write, return a client error, and audit the refusal with the detected secret type.
- **REQ-134** (story US-3, Must): WHEN an operator requests the prompt list for an agent THEN the system SHALL return every prompt across every installed Arc package, each marked as stock or overridden, including prompts that have no overlay.
- **REQ-135** (story US-2, Must): WHEN an operator requests a single prompt THEN the system SHALL return its stock body, its effective body, and a unified diff between them.
- **REQ-136** (story US-2, Must): WHEN an operator deletes an overlay THEN the system SHALL remove the overlay file so the prompt resolves to stock on the next run.
- **REQ-137** (story US-1, Should): WHERE the operator is in operator mode, arcui SHALL present a Prompts tab listing prompts grouped by package with a stock or overridden indicator, and a drawer offering stock, effective, and diff views plus edit and reset actions.
- **REQ-138** (story US-4, Must): WHEN a prompt is externalized from a Python constant THEN a test SHALL assert the loaded markdown equals the original constant byte-for-byte, and the constant SHALL NOT be deleted until that assertion passes.
- **REQ-139** (story US-3, Should): WHERE a deployment's policy declares a rule denying prompt writes, the system SHALL refuse overlay writes through the existing policy pipeline without introducing tier-conditional branching into arcprompt.
- **REQ-140** (story US-4, Must): The build SHALL include every packaged prompt markdown file in the distributed wheel for each package that ships prompts.

## MoSCoW Priorities

| Priority | Requirements |
|---|---|
| Must | REQ-121, REQ-122, REQ-123, REQ-124, REQ-125, REQ-126, REQ-127, REQ-128, REQ-129, REQ-130, REQ-131, REQ-132, REQ-133, REQ-134, REQ-135, REQ-136, REQ-138, REQ-140 |
| Should | REQ-137, REQ-139 |
| Could | _(none)_ |
| Won't | _(none)_ |

## Success Metrics

Framework: `.claude/steering/product.md#success-metrics-framework`. Targets for this feature — (1) time to change deployed harness behavior drops from an edit-build-redeploy cycle to a single write plus next run; (2) 100% of externalized prompts pass byte-identity assertions before their constants are deleted; (3) every prompt in the repo is enumerable through the API, with zero prompts remaining as Python string literals in the four migrated packages at completion; (4) every run emits exactly one prompt-provenance audit event; (5) package quality gates hold — line coverage >= 80%, branch >= 75%, ruff and mypy --strict clean, arcrun stays within its 5,400 NCLOC foundation budget.

## Risks and Constraints

Byte-identity is fragile in a known way: `workpad/prompt.py` suppresses a leading newline with a backslash-continued quote while `Path.read_text()` returns a trailing newline verbatim, so newline handling must be decided explicitly (REQ-138 exists to catch this). `CONTEXT_MAINTAINER_SYSTEM_PROMPT`, the largest prompt at ~4.9 KB, has zero test coverage today, so tests must be authored against the constant before it moves. Packaged markdown is silently dropped from wheels unless declared (REQ-140) — the lesson from the SPEC-047 blueprint migration. Signing lands friction on the default path because `tier = "personal"` is hardcoded in eight places in the agent-create scaffold. Until arcui gains per-user login, holding the shared operator token confers signing authority; this still improves on today's baseline, where identity.md is writable through that same token and carries no signature at all. arcrun mounts identity.md and context.md read-only in the docker backend, so overlay visibility to a live agent must be verified against REQ-123's next-run assumption. A new leaf package requires a hand-written architecture import guard, since leaf-ness is not generically enforced. Constraints: `.claude/steering/tech.md#compliance` and `product.md#business-constraints`.

## Open Questions

- Signature storage: detached `.arcsig` sidecar (matches arc's existing convention, no canonicalization risk, but can desync from its file) versus a frontmatter field using git's gpgsig excluded-field pattern (cannot desync, but no established convention exists for signing markdown+frontmatter and canonicalization traps apply). - lets keep it detached
- Overlays live under a gitignored path, so `git history` — the mechanism REQ-127 relies on for stock version history — does not exist for the overlay half. Either overlays gain store-backed history or the audit trail is the sole record of change. lets build the overlay mechanics
- Whether `tunable: false` requires any enforcement in v1 or remains inert metadata until the v2 optimizer exists. - tunable is nothing right now.
- Whether identity.md's absent integrity check (confirmed, not a design choice) is closed in this spec or tracked separately. leave it for now, not realted to prompt management
- Whether the arcskill improver's four per-call prompt builders belong in arcprompt at all, given they interpolate trace data rather than being authored harness text. - arcskill and arcprompt are completely different and not to cross. you can use concpets, but not the actual code. 
