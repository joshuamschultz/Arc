# Product Requirements Document: Skill-Improver Eval Bootstrap (SPEC-054)

## Context References

- **Personas:** [.claude/steering/product.md#user-personas](../../steering/product.md#user-personas)
- **Constraints:** [.claude/steering/product.md#business-constraints](../../steering/product.md#business-constraints)
- **Metrics Framework:** [.claude/steering/product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework)
- **Current Phase:** [.claude/steering/roadmap.md#current-phase](../../steering/roadmap.md#current-phase)

## Product Overview

### Vision
Arc's skill improver (SPEC-044) becomes a self-improving system that just works behind the scenes: every skill acquires a trustworthy, machine-verified golden-task suite automatically, the improver learns from real user outcomes instead of tool-error counts, and every mutation remains gated, attributed, and audited per tier.

### Problem Statement
The golden-task gate shipped by SPEC-044 is structurally inert: no skill has an evals/ suite, so personal-tier prose mutations auto-accept on LLM-judge opinion alone and code mutations are permanently blocked. Worse, create_skill scaffolds an always-passing placeholder that makes load_suite non-empty, silently bypassing the fail-closed no-suite policy at enterprise/federal. The improver's outcome signal is mechanical (tool-error counting) — a skill that runs cleanly but does the wrong thing is recorded as success, and the schema's evaluator outcome slot has no producer.

### Value Proposition
Makes the SPEC-044 investment real: strict-improvement gating with actual ground truth, self-improvement driven by user-perceived outcomes, and operator surfaces (CLI + arcui) to review generated evals and skill evolution. No shipped competitor combines automatic and eval-gated improvement (Hermes ships one or the other); this is a differentiator for the federal 'easy button' vision.

## Personas

See [.claude/steering/product.md#user-personas](../../steering/product.md#user-personas). Primary: Agent Developer (skills improve without hand-written tests). Secondary: Federal/Regulated Security Architect (autonomous mutation stays fail-closed, attested, tier-gated). Tertiary: Auditor/Reviewer (every generation, label, toggle flip, and rollback leaves a tamper-evident trail).

## User Stories

- **US-1**: As an agent developer, I want skills to acquire machine-verified golden eval suites automatically, so that self-improvement gates on ground truth without me hand-writing pytest cases..
- **US-2**: As an agent developer, I want the improver to learn whether a skill actually did what the user wanted, so that improvement targets real failures, not just tool exceptions..
- **US-3**: As an operator, I want CLI and arcui surfaces to view/edit generated evals and browse skill versions with diffs, so that I can correct off-target generation and audit how skills evolve..
- **US-4**: As a federal security architect, I want every self-improvement mechanism tier-gated, provenance-marked, and audited, so that autonomous skill mutation is defensible under NIST 800-53 (AU-2, CM-5, SI-12)..

## Functional Requirements

- **REQ-101** (story US-1, Must): WHEN maybe_improve fires for a skill whose evals/ suite is empty or placeholder-only THEN the system SHALL generate candidate golden cases from the skill's declared Contract, Examples, and Validation sections plus its script files, via the injected LLMInvoker seam, before any gate decision runs.
- **REQ-102** (story US-1, Must): The system SHALL validate every generated case through the ordered cascade — ast.parse, anti-tautology static check (reject assert-free and assert-True-class bodies), then N=5 sandboxed executions against the current bundle — and SHALL adopt only all-stage passers as regression anchors.
- **REQ-103** (story US-1, Must): IF a generated case fails against the current bundle THEN the system SHALL quarantine it as an untrusted improvement target and SHALL NOT admit it to the eval gate without human review.
- **REQ-104** (story US-1, Should): The system SHALL run a negative-control mutation probe at adoption time and SHALL quarantine any candidate case that still passes against a deliberately mutated copy of the skill's scripts.
- **REQ-105** (story US-1, Must): WHEN create_skill completes THEN the scaffold SHALL contain either a generated suite that passed the validation cascade or no evals file at all; the system SHALL NOT write placeholder tests under any fallback path, including LLM unavailability.
- **REQ-106** (story US-1, Should): WHEN a mutation is applied to a skill THEN the system SHALL schedule an add-only suite extension as a background task; suite extension SHALL never regenerate or remove existing adopted anchors.
- **REQ-107** (story US-1, Should): The hourly Curator sweep SHALL include a backstop pass that generates suites for suite-less skills, ordered most-used-first, with per-skill jitter and immediate short-circuit when no skill needs work.
- **REQ-108** (story US-1, Must): The system SHALL serialize suite generation and improvement passes per skill via single-flight locking so an eval-gate decision never reads a suite that is being written.
- **REQ-109** (story US-4, Must): The generating harness SHALL mark machine-authored case files with an @generated marker and a manifest provenance entry written outside model-controlled content; the generation LLM SHALL NOT be able to assert or remove provenance itself.
- **REQ-110** (story US-4, Must): WHILE the constructed tier is enterprise or federal, the eval gate SHALL count only human-authored cases toward min_golden_cases; machine-authored cases SHALL be supplemental and SHALL count at personal tier.
- **REQ-111** (story US-4, Should): WHEN a human edit modifies a machine-authored eval file THEN the system SHALL strip the @generated marker and reclassify the file as human-authored.
- **REQ-112** (story US-4, Must): The system SHALL expose suite-generation settings as a [modules.skills.improver.suite] Pydantic sub-block with extra='forbid' (autogen, min_cases, max_cases, generate_on_create, extend_after_mutation), overridable per skill via the existing frontmatter improver: block.
- **REQ-113** (story US-4, Must): WHEN any self-improvement toggle changes state THEN the system SHALL emit an audited config_change event recording from-value, to-value, actor identity, and timestamp, fired before the change takes effect and at most once per flip.
- **REQ-114** (story US-4, Must): The system SHALL resolve toggle precedence deterministically with global-off dominating per-skill-on; an overridden per-skill enable SHALL be audited as OVERRIDDEN, and exempt tags SHALL never re-enable a globally disabled subsystem.
- **REQ-115** (story US-2, Must): WHEN a turn ends and cheap heuristic pre-filters detect a candidate implicit-feedback signal THEN an arcagent-side classifier SHALL produce a task outcome label (success, failure, partial, or abstain) and forward it through the existing on_turn_end outcome parameter, entirely off the agent respond path.
- **REQ-116** (story US-2, Must): The classifier SHALL bind a failure label to a named skill only when attribution is explicit and SHALL abstain otherwise; user silence SHALL never be labeled failure; positive labels SHALL pass a safety check before the improver consumes them.
- **REQ-117** (story US-2, Should): WHERE arg capture is enabled by configuration, the system SHALL scrub tool-call args at the observe boundary before any persistence; hash-only capture SHALL remain the non-overridable federal default.
- **REQ-118** (story US-2, Should): The system SHALL promote evaluator-labeled successful traces into deterministic replay anchors and observed failures into repro cases, each pinned to the skill_version it was captured against, with an expiry path that retires obsolete cases visibly.
- **REQ-119** (story US-3, Must): The arc skill command group SHALL provide evals list, regen, and edit subcommands where edit uses a validate-on-save loop ($EDITOR, reject-and-reopen preserving edits) and atomic temp-file + os.replace commits, warning before an edit reduces the passing-anchor count or drops a suite below min_golden_cases.
- **REQ-120** (story US-3, Should): arcui SHALL render a per-skill read-only eval-case view and a version timeline with side-by-side diffs and confirm-gated, audited rollback, all pulled from arcstore ingestion of the on-disk candidate store.

## MoSCoW Priorities

| Priority | Requirements |
|---|---|
| Must | REQ-101, REQ-102, REQ-103, REQ-105, REQ-108, REQ-109, REQ-110, REQ-112, REQ-113, REQ-114, REQ-115, REQ-116, REQ-119 |
| Should | REQ-104, REQ-106, REQ-107, REQ-111, REQ-117, REQ-118, REQ-120 |
| Could | _(none)_ |
| Won't | _(none)_ |

## Success Metrics

Referenced framework: [.claude/steering/product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework). Targets: 100% of improvement passes gated by a real (non-placeholder) suite; >80% of active skills hold >=3 adopted anchors after 10 sessions of use; 0 placeholder eval files anywhere post-migration; generated-case discard rate observable via audit (expected 40-60% per TestGen-LLM baselines); classifier abstain rate monitored with alerting on drift; every toggle flip and rollback present in the WORM audit chain.

## Risks and Constraints

Bug-freezing (generated oracles encode actual not intended behavior) — mitigated by grounding expected values in declared Contract sections and labeling run-inferred expectations change-detectors (arXiv 2410.21136). Marker spoofing by the generation LLM — mitigated by harness-written manifest provenance (REQ-109). Classifier mislabeling steering improvement wrong — mitigated by noisy-signal aggregation thresholds, abstain default, and safety-checked positive labels (REQ-116). Suite generation racing the gate — mitigated by per-skill single-flight (REQ-108). LLM cost blowout — mitigated by bounded candidate budgets, cheap-filter-first ordering, and semaphore caps. Silent suite shrink via load_suite's SyntaxError tolerance — mitigated by validate-on-save (REQ-119).

## Open Questions

_(none)_
