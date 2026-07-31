# SPEC-044 — Best skill builder/improver — PRD

**Status:** COMPLETE
**Type:** Concern relocation (arcagent → arcskill) + capability expansion (code-repair, eval-gate, retire loop) + optional harness extension seam
**Phase:** Phase 2 — SOTA + mission control (`ROADMAP-PROGRAM.md`)
**Owner:** Josh (product owner)
**Coding identity:** principled-coder — Simplicity → Modularity → Security → Scalability (in that order)

---

## 1. Problem

Arc already has an evolutionary **skill improver** — but it lives in the wrong place and does the wrong half of the job.

**Wrong place.** ~3,150 LOC of skill-improvement *logic* (mutation engine, evaluator, guardrails, candidate store, Pareto frontier, nudge emitter) sits inside `packages/arcagent/src/arcagent/modules/skill_improver/`. Per Josh's ruling (2026-07-08): **arcskill IS the optional package** — arcagent already manages skills (writes, loads, runs) on its own; arcskill isn't needed for that, but installing it **supercharges** skills (adaptation, improvement, skill management). The improvement logic therefore belongs in **`arcskill.improver`** (a subpackage beside `hub/`, driving hub's `dry_run` sandbox + verify gate internally). arcagent owns only the **wiring** (the module-bus hooks that observe usage) and a **config-enabled extension seam**, mirroring the `Brain` Protocol that SPEC-041 established for memory (where `arcmemory` is likewise a separate, optional package — not folded into arcagent). Keeping the logic in arcagent bloats the harness and couples skill improvement to the agent core; within arcskill, improvement still activates only via config, so installing arcskill does not force the self-modification surface on.

**Wrong half.** Three capability gaps make today's improver unfit for federal self-modification:

1. **Prose-only.** The reflector rewrites the SKILL.md *body text* and nothing else (`reflector.py` → whole-body markdown replacement). It cannot repair the **code** inside a skill's scripts — where most real skill failures actually live (a broken `subprocess` call, a wrong flag, an unhandled error path). The trace already captures `error_type` per tool call; the improver ignores it as a code-repair signal.
2. **Judge-only acceptance.** A mutation is accepted on an **LLM-as-judge score delta** (`engine.py` → `frontier.add_if_improves`). An LLM judge cannot *validate that code is correct*. There is no deterministic **golden-task eval suite** gating mutations — so a "better-scoring" mutation can silently break behavior. This is unacceptable for auditable self-modification.
3. **Half a lifecycle.** Only the **create-nudge** exists (`nudge_emitter.py` — advisory "consider making a skill"). Skills accrue usage/outcome data but nothing acts on it: underperformers are never nudged for improvement, and unused/persistently-failing skills never **retire**. There is no closed **nudge → usage → retire** loop.

**Why now.** SPEC-041 (arcmemory) just merged and **unblocks SPEC-044**. It delivered the exact seam pattern we mirror (`Brain` Protocol + `NullBrain` default + config-select + BYO signing gate) and the reflection/insight signals the improver should *consume rather than rebuild*. The roadmap's next explicit ask is "**build SPEC-044 then SPEC-047**."

---

## 2. Goals & non-goals

### Goals
- Relocate all skill-improvement **logic** to `arcskill`; leave only thin, config-enabled **wiring** in arcagent (net arcagent LOC **down**).
- Expand mutation from prose-only to **prose + code repair**, over the whole skill bundle (SKILL.md + scripts).
- Make **golden-task evals a hard gate**: no eval suite → mutation blocked (fail-closed at enterprise/federal); a mutation applies only if it passes the suite (regression-safe).
- Add **first-class change-bound configuration** (SkillOpt-style bounded per-step change), tunable per tier **and** per skill, layered on the existing guardrails.
- Close the **nudge → usage → retire** loop as an audited skill-lifecycle state machine.
- Every mutation: **bounded, eval-gated, operator-signed-audited, agent-DID-re-signed, reversible**.
- Expose skill self-adaptation as an **optional core extension** to arcagent (config-enabled, not hardwired) via a `SkillAdapter` seam mirroring `Brain`.

### Non-goals
- **Not** rebuilding reflection/insight analysis — consume arcmemory's insight signals where a Brain is active; work fully without memory otherwise.
- **Not** unattended autonomous mutation at federal — federal requires operator approval per mutation (§7 tier table).
- **Not** a new skill-authoring UX — the "builder" side of scope means (a) the existing create-nudge and (b) ensuring newly-built skills ship a golden-task eval scaffold so they are *improvable*. Full authoring tooling is skill-creator's domain.
- **Not** SPEC-047 — SPEC-047 *generalizes* the `SkillAdapter`/`Brain` seams into a first-class extension-point framework. SPEC-044 only *delivers* the skill seam in that shape.

---

## 3. Users & value

| User | Value |
|---|---|
| **Agent operator (enterprise/federal)** | Skills that measurably improve over time under **bounded, gated, signed, reversible** self-modification they can audit and roll back — safe enough for a SCIF. |
| **Agent (runtime)** | Fewer repeated failures: broken skill code gets repaired, dead skills retire, recurring workflows get suggested as skills. |
| **Skill author** | Skills ship with golden-task evals; the improver maintains them without silent regressions. |
| **Compliance / auditor** | A complete, operator-signed, tamper-evident lifecycle trail (NIST AU-2/AU-3/AU-10, CM-8) for every skill mutation and state transition. |

---

## 4. Functional requirements (EARS)

Priorities: **M**=Must, **S**=Should, **C**=Could, **W**=Won't (this spec).

### Relocation & seam
- **REQ-001 (M)** — The system SHALL host all skill-improvement logic (trace analysis, mutation, evaluation, guardrails, candidate store, lifecycle) in **`arcskill.improver`** — a subpackage of the optional arcskill package, beside `hub/`, and NOT in arcagent. arcskill as a whole remains optional: arcagent runs skills without it. *(Modularity, Simplicity — Josh's ruling 2026-07-08)*
- **REQ-002 (M)** — arcagent SHALL define a structural **`SkillAdapter` Protocol** and a no-op **`NullSkillAdapter`** default; when the default is active, skill improvement is a **silent no-op** (no traces stored, no mutations, no files written). *(Simplicity, Modularity)*
- **REQ-003 (M)** — WHEN skill improvement is enabled by config, the system SHALL select the adapter implementation (`none` / `arcskill` / a custom class-path) via **lazy import** (no static arcagent→arcskill dependency, mirroring `select_brain`); IF a custom class-path is given, THEN the system SHALL verify it is signed/allowlisted before load and SHALL fail-closed at enterprise/federal on an unsigned adapter. IF `arcskill` is not installed, THEN improvement SHALL degrade to the silent `NullSkillAdapter` no-op (never a crash). *(Security — mirrors SPEC-041 BYO-brain RCE gate)*
- **REQ-004 (M)** — The `arcskill.improver` subpackage SHALL NOT import `arcagent`, `arcllm`, or `arcmemory` (it MAY use sibling `arcskill.hub` for the sandbox/verify/bundle model and `arctrust` for sign/audit); all provider/LLM/eval/audit/signing dependencies SHALL enter through injected Protocol seams. *(Modularity — mirrors arcmemory `Distiller`)*

### Code-repair mutation
- **REQ-010 (M)** — The improver SHALL support mutating a skill **bundle** across both its `SKILL.md` prose and its executable **script code**, as a structured multi-file patch. *(the headline capability)*
- **REQ-011 (M)** — WHEN failure traces attribute errors to a skill's tool/script calls (`error_type` present), the improver SHALL generate a candidate **code patch** targeting the failing behavior. *(Security — LLM05 improper-output handling: patches are candidates, never executed unvalidated)*
- **REQ-012 (M)** — A candidate patch SHALL be re-signed as a whole bundle (agent DID) and SHALL re-verify through the existing `arcskill.hub` load gate before it can take effect. *(Security — SPEC-033)*
- **REQ-013 (S)** — Prose-only mutation of `SKILL.md` SHALL remain available (the existing capability), subject to the same gates.

### Golden-task eval gate
- **REQ-020 (M)** — An improvable skill SHALL carry a **golden-task eval suite** (deterministic input → expected-outcome cases) in its bundle. *(Simplicity — deterministic gate)*
- **REQ-021 (M)** — IF a skill has no golden-task eval suite, THEN the improver SHALL block mutation at enterprise/federal (fail-closed) and MAY allow prose-only mutation with an audit-warning at personal; it SHALL NEVER allow **code** mutation without evals at any tier. *(Security)*
- **REQ-022 (M)** — A candidate SHALL be applied ONLY IF it passes the full golden-task suite with no regression against the currently-active version; the golden-task result is the **hard gate**, and the LLM-judge score is used only to rank candidates within the frontier. *(Security, Simplicity)*
- **REQ-023 (M)** — WHEN the improver runs a skill's golden-task suite or generates/validates a code patch, it SHALL execute untrusted code inside the SPEC-036 sandbox at enterprise/federal (fail-closed if unavailable). *(Security — ASI05 RCE)*

### Bounded change (SkillOpt)
- **REQ-030 (M)** — The system SHALL expose **change-bound settings** — how much a skill may change per optimization step — as first-class config, layered on the existing anchor-distance / token-budget / oscillation guardrails. *(Security — bounded self-modification)*
- **REQ-031 (M)** — Change-bound settings SHALL be tunable **per tier** (federal tightest, non-relaxable below the federal floor) **and per skill** (override within the skill's tier ceiling). *(Modularity)*
- **REQ-032 (S)** — The bounding method and its constant values SHALL adopt Microsoft **SkillOpt** [DEEPEN — /deepen pins parameters from the paper]. *(Correctness)*
- **REQ-033 (M)** — The existing guardrails (immutable-intent preservation, token-ratio, anchor-distance, oscillation, cooloff, exempt-tags, generation cap) SHALL be retained and applied to every candidate. *(Security)*

### Nudge → usage → retire lifecycle
- **REQ-040 (M)** — The create-**nudge** (advisory suggestion to author a skill for a recurring, uncovered, successful workflow) SHALL be retained. *(ASI09 — advisory only, never auto-creates)*
- **REQ-041 (M)** — Each skill SHALL accrue **usage/outcome statistics** (invocation count, success/failure/partial rate, recurring error signatures, last-used turn). *(Simplicity)*
- **REQ-042 (M)** — WHEN a skill's outcome statistics fall below the underperformance threshold, the system SHALL emit an **improve-nudge** and enqueue a gated improvement candidate. *(the loop)*
- **REQ-043 (M)** — WHEN a skill is unused beyond the inactivity window OR remains below the failure floor after the configured number of improvement attempts, the system SHALL **retire** it (disable, retain lineage — reversible; never destructive-delete). *(the loop; reversibility)*
- **REQ-044 (S)** — An operator SHALL be able to **revive** a retired skill, restoring it from candidate-store lineage. *(reversibility)*
- **REQ-045 (M)** — The lifecycle SHALL be a defined state machine (`active → nudged/improving → active | underperforming → retired ⇄ revived`); every transition SHALL emit an audit event. *(Simplicity, auditability)*

### Signing, audit, reversibility
- **REQ-050 (M)** — Improver **audit events** (mutation applied, retire, revive, nudge, rollback) SHALL be signed with the **operator key** (SPEC-053) and routed to the tamper-evident WORM chain; **skill artifact signatures** SHALL remain **agent DID** (SPEC-033). *(Security — SPEC-053 directive)*
- **REQ-051 (M)** — The candidate store SHALL retain lineage (seed + every candidate + parent links + scores) enabling **rollback** to any prior version. *(reversibility)*
- **REQ-052 (M)** — Rollback SHALL restore the prior bundle, re-verify its signature, set a cooloff, and emit an operator-signed audit event. *(Security)*

### Optional enrichment & observability
- **REQ-060 (S)** — WHEN an arcmemory-compatible `Brain` is active, the improver SHALL be able to consume its **insight** signals (recurring-failure abstractions) to inform mutation, passed in as primitive text through the adapter; the improver SHALL work fully with no memory present. *(Modularity — no arcmemory import)*
- **REQ-061 (C)** — The skill-lifecycle state and audit trail SHALL be exposable to arcui as a read surface (conditional on an active improver; adapts/hides otherwise). *(deferred surface — SPEC-032 territory)*

### Builder side
- **REQ-070 (S)** — Skills newly authored through the build path SHALL ship a **golden-task eval scaffold** so they are improvable from creation. *(closes the builder→improver handoff)*

---

## 5. Non-functional requirements

- **NFR-001 (M)** — arcagent package NCLOC SHALL **decrease** relative to `develop` as improver logic moves out; arcagent `core/*.py` SHALL remain **< 3,500** NCLOC. *(Simplicity)*
- **NFR-002 (M)** — `ruff check` clean and `mypy --strict` clean across arcskill, arcagent, and any touched package (no pre-existing errors left behind — CLAUDE.md). *(Simplicity)*
- **NFR-003 (M)** — TDD: a failing test precedes every implementation unit; the full per-package test matrix passes at each phase boundary.
- **NFR-004 (M)** — No provider dependency in `arcskill.improver`; all LLM/eval/sandbox/audit access via injected seams (arcskill may depend on arctrust only). *(Modularity)*
- **NFR-005 (M)** — Background improvement SHALL never block or crash the agent loop (bounded concurrency, caught+audited failures — as today's `spawn_background`). *(Scalability)*
- **NFR-006 (M)** — Fail-closed on every security gate (missing eval suite, missing sandbox, unsigned adapter, missing operator key at federal). *(Security)*

---

## 6. Acceptance criteria (headline — full E2E in PLAN)

- **AC-1** — `pip install arcagent` alone (no `arcskill` installed) → agent runs, skill improvement is a **silent no-op**, zero improver files written; with arcskill installed but improver not enabled in config → same silent no-op. *(REQ-002)*
- **AC-2** — With `arcskill` installed and the improver enabled: a **real agent run** exercising a skill with a seeded code bug → usage telemetry accrues → the improver generates a **code patch** → the patch passes the skill's **golden-task suite** → bundle re-signed (agent DID) → re-verified through the hub gate → reloaded → the previously-failing golden task now passes. All observed through the **real production path**, not a fixture. *(REQ-010/011/012/022/023 — producers-unwired defense)*
- **AC-3** — A skill with **no** golden-task suite → code mutation **blocked** at enterprise/federal (audit event proves the block); personal prose-only mutation emits an audit-warn. *(REQ-021)*
- **AC-4** — A candidate exceeding the tier/skill **change-bound** is rejected before evaluation, with an audit event. *(REQ-030/031)*
- **AC-5** — A skill unused past the inactivity window retires (disabled, lineage retained); an operator revive restores it; both transitions are operator-signed audit events. *(REQ-043/044/045/050)*
- **AC-6** — Every mutation and lifecycle transition produces an **operator-signed** WORM audit event while the mutated bundle's sidecar signature is **agent DID**. *(REQ-050)*
- **AC-7** — arcagent package NCLOC is lower than `develop`; core `<3500`; all touched packages ruff + mypy-strict clean. *(NFR-001/002)*

---

## 7. Tier stringency table (ADR-019 — tier = stringency, not gate)

Every tier verifies, authorizes, audits, and identifies. Tiers differ only in stringency.

| Concern | Personal | Enterprise | Federal |
|---|---|---|---|
| Improver enabled | Opt-in via config | Config | Config (FIPS crypto) |
| Prose mutation w/o eval suite | Allowed + **audit-warn** | **Blocked** | **Blocked** |
| Code mutation w/o eval suite | **Blocked** | **Blocked** | **Blocked** |
| Golden-task regression gate | Enforced | Enforced | Enforced |
| Eval / patch execution sandbox | Best-effort (host allowed) | **SPEC-036 sandbox, fail-closed** | **SPEC-036 sandbox, fail-closed** |
| Mutation apply approval | Auto (audited) | Auto for prose; **operator-approve code** | **Operator-approve EVERY mutation** (mirror SPEC-043 federal ladder) |
| BYO/custom adapter | Self-signed OK (audit-warn) | **Signed + allowlisted** | **Signed + allowlisted** |
| Change-bound floor | Relaxable | Relaxable above federal floor | **Federal floor, non-relaxable** |
| Audit signing authority | Operator key | Operator key | Operator key + FIPS |
| Retire approval | Auto (audited) | Auto (audited) | **Operator-approve** |

*(Values marked [DEEPEN] for the change-bound floor pinned during /deepen from SkillOpt.)*

---

## 8. Dependencies & references

- **SPEC-041 (arcmemory)** — the seam pattern mirrored here (`Brain` Protocol + `NullBrain` + config-select + BYO signing gate); the insight/reflection signals optionally consumed (REQ-060). *Unblocks this spec.*
- **SPEC-033 (Sign pillar)** — `arctrust.sign_artifact/verify_artifact` + `arcagent.capabilities.artifact_signing` sidecar convention; re-sign/re-verify on mutation (REQ-012/016).
- **SPEC-053 (audit-authority independence)** — operator key signs audit; skill sig stays agent DID (REQ-050).
- **SPEC-036 (code-exec sandbox)** — sandboxed eval/patch execution (REQ-023).
- **SPEC-043 (loop controls / HITL)** — the tier approval ladder reused for per-mutation operator approval (§7).
- **SPEC-047 (extensibility)** — will generalize the `SkillAdapter`/`Brain` seams; SPEC-044 delivers the skill seam in that shape.
- **SPEC-012 (skill improver, COMPLETE)** — the existing arcagent-modules implementation this spec **relocates and supersedes**.
- **Microsoft SkillOpt (2025-26)** [DEEPEN], **Hermes self-adaptation** [DEEPEN] — external research folded in /deepen.
