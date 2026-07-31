# SPEC-044 — Best skill builder/improver

**Feature:** Extract Arc's evolutionary **skill improver** from `arcagent/modules/` into **`arcskill.improver`**. arcskill IS the optional supercharger package (mirroring how `arcmemory` is a separate optional package): arcagent already manages skills (writes, loads, runs) on its own; arcskill isn't needed for that, but installing it supercharges skills — adaptation, improvement, skill management. Expand it from a prose-only, judge-gated optimizer into a **code-repairing, golden-task-gated, bounded, reversible** self-modification system that pairs **GEPA-style trace reflection** (proposal) with **SkillOpt bounded-edit optimization** (gate) as one integrated method, driving a full **nudge → usage → retire** skill lifecycle that runs **behind the scenes** — the operator just sees a better agent. Exposed to the harness as an **optional, config-enabled extension** through a `SkillAdapter` seam that mirrors SPEC-041's `Brain` Protocol. Install it to turn self-improvement on; omit it and skills simply install/verify/run.

**Status:** COMPLETE
**Branch:** `feat/SPEC-044-skill-improver`
**Type:** Concern relocation + capability expansion + optional harness extension seam
**Phase:** Phase 2 — SOTA + mission control (`ROADMAP-PROGRAM.md`)
**Supersedes:** **SPEC-012 (skill improver, COMPLETE)** — its arcagent-modules-only architecture is extracted into the optional `arcskill` package (`arcskill.improver`); its trace/candidate/guardrail/audit design is retained and absorbed.
**Unblocks / feeds:** **SPEC-047** (extensibility) — SPEC-044 delivers the `SkillAdapter` seam in the exact shape SPEC-047 will generalize into a first-class extension point.
**Depends on / references:** SPEC-041 (Brain seam pattern + insight signals), SPEC-033 (sign/verify sidecar), SPEC-053 (operator-key audit authority), SPEC-036 (code-exec sandbox), SPEC-043 (tier HITL ladder).

---

## Why now

The roadmap's explicit next ask is *"build SPEC-044 then SPEC-047."* SPEC-041 just merged and unblocks this: it established the mirror-able seam (`Brain` Protocol + `NullBrain` default + config-select + BYO signing gate), reduced arcagent to memory-less-by-default, and now emits the insight/reflection signals the improver should *consume, not rebuild*. Meanwhile the existing improver (SPEC-012, ~3,150 LOC in `arcagent/modules/skill_improver/`) is in the wrong package (bloating the agent core) and does the wrong half of the job — prose-only mutation, LLM-judge-only acceptance, and only a create-nudge (no usage-driven improve/retire). It belongs in **arcskill** — the optional supercharger package you install to add skill adaptation/improvement/management — so self-improvement is a capability you add, not a cost every deployment pays (arcagent runs skills fine without arcskill installed).

## What changes (three moves)

1. **Extract into the optional arcskill package.** All improvement *logic* → **`arcskill.improver`** subpackage (beside `hub/`; uses hub's `dry_run` sandbox + verify gate internally, arctrust for sign/audit; imports no arcagent/arcllm/arcmemory). arcagent keeps only `arcagent/skilladapt/` — a `SkillAdapter` Protocol + `NullSkillAdapter` default + thin hook/wiring, mirroring `arcagent/brain/`, config-selecting `none`/`arcskill`/signed BYO via lazy import (the arcmemory pattern; no static arcagent→arcskill dependency). Net arcagent LOC **down**. The old `modules/skill_improver/` tree is **deleted in the same change** (no-legacy).
2. **Expand.** Prose-only → **prose + code-repair** over the whole skill bundle; LLM-judge-only → **deterministic golden-task eval gate** (judge only ranks); unbounded-ish → first-class **change-bound config** (SkillOpt) per tier + per skill.
3. **Close the loop.** create-nudge only → full **nudge → usage → retire** lifecycle state machine, every transition operator-signed-audited, every mutation bounded/eval-gated/re-signed/reversible.

## Concern boundary (load-bearing)

- **`arcskill.improver`** — pure logic (trace store, mutate, eval-gate, guardrails+change-bound, candidate store, lifecycle). Imports **no** arcagent/arcllm/arcmemory; all deps via injected `Mutator`/`Judge`/`EvalRunner`/`Signer`/`AuditSink` Protocol seams (the arcmemory `Distiller` precedent). Default `EvalRunner` = thin adapter over sibling `arcskill.hub.dry_run` (DC-5).
- **`arcagent.skilladapt`** — Protocol + `NullSkillAdapter` + `select` + `extension` (bus hooks forward primitive signals; proactive engine schedules; injects arcllm/sandbox/operator-audit/agent-DID seams). Improvement is a **silent no-op by default**.

See SDD §2 for the full boundary table and SDD §4 for the component/file map.

## Design decisions (locked defaults — Fable, 2026-07-08)

| ID | Decision | Rationale / pillar |
|---|---|---|
| **D-1** | Improver logic lives in **`arcskill.improver`** (subpackage beside `hub/`); arcagent keeps only `skilladapt` (Protocol + Null + wiring). **arcskill IS the optional supercharger package** — arcagent manages skills (writes, loads, runs) without it; installing arcskill adds adaptation/improvement/skill management. | Modularity + Simplicity — self-improvement is an add-on capability, not a cost every deployment pays; exact mirror of `arcmemory` (separate optional package) ↔ arcagent (`Brain` seam). **[FINAL 2026-07-08 per Josh: "arcskill IS the optional package; arcagent already manages skills; arcskill supercharges them." Supersedes the misread `arcevolve` sibling-package revision.]** |
| **D-1b** | `arcskill.improver` uses sibling `arcskill.hub` (dry_run sandbox + verify gate + bundle model) and **arctrust** (sign/audit); imports no arcagent/arcllm/arcmemory. arcagent→arcskill is a **lazy config-select import** only (no static dep). | Modularity — provider-free, arch-test-clean; `pip install arcagent` without arcskill = skills run, self-improvement silent no-op (AC-1). |
| **D-1c** | Improvement is **one integrated method**: GEPA-style trace-reflection proposal → SkillOpt bounded-edit + strict-improvement golden-task gate → apply. Not two parallel systems. | Correctness — both mechanisms are academically grounded (GEPA published; SkillOpt arXiv:2605.23904, 52/52); they compose in a single `optimize()` pass. |
| **D-2** | `SkillAdapter` Protocol + `NullSkillAdapter` default; config-selects `none`/`arcskill`/signed BYO. | Simplicity + Security — improver-less by default; BYO signing gate stops RCE (SPEC-041 review). |
| **D-3** | arcskill stays **provider-free**; LLM/eval/sign/audit enter via injected Protocol seams. | Modularity — arch-test-clean, deterministic-testable. |
| **D-4** | **Golden-task evals are the hard acceptance gate**; LLM-judge only ranks the frontier. | Security — a judge can't validate code correctness. |
| **D-5** | Code mutation **requires** an eval suite at every tier; prose may mutate eval-less only at personal (audit-warn). | Security — code without a deterministic gate is unsafe self-modification. |
| **D-6** | Untrusted code (eval suite, patch validation) runs in the **SPEC-036 sandbox**; fail-closed at ent/federal. | Security — ASI05 RCE. |
| **D-7** | **Change-bound** is first-class config, tunable per tier + per skill, federal floor non-relaxable; values **[DEEPEN]** from SkillOpt. | Security — bounded, auditable self-modification. |
| **D-8** | **Retire = disable + retain lineage** (reversible), never destructive-delete. | Reversibility — no-legacy applies to *our* code, not the user's skills. |
| **D-9** | Improver **audit** signed by **operator key** (SPEC-053); **skill artifact** signed by **agent DID** (SPEC-033). | Security — audit authority ≠ audited subject. |
| **D-10** | Federal requires **operator approval per mutation** and per retire/revive (SPEC-043 ladder); enterprise approves code mutations; personal auto+audit. | Security — excessive-agency control. |
| **D-11** | arcmemory insight enrichment is **optional**, passed as primitive text through the adapter; improver works fully memory-less. | Modularity — no arcmemory import. |
| **D-12** | Every mechanism has an **E2E-through-real-path** acceptance test (not a rigged fixture). | Anti producers-unwired ([[feedback_producers_unwired_pattern]]). |

## Josh's calls (2026-07-08, deepen boundary — LOCKED)

- **Package ruling (FINAL, supersedes the `arcevolve` misread):** "arcskill IS the optional package; arcagent already manages skills (writes, loads, etc); arcskill is the optional package that isn't needed, but supercharges skills (adds adaptation, improvement, skill management, etc)." → improver = `arcskill.improver`; NO new arcevolve package; optionality lives at the arcskill-package boundary via the `skilladapt` lazy config-select seam.
- **OQ-2 → H-A + H-B both ADOPTED.** GEPA trace-reflection mutation proposer + autonomous Curator usage-sweep. **Sweep window default = 30 days**, and all sweep settings (window, thresholds, schedule) MUST be adjustable in `config.toml`. Curator auto-merge stays deferred; H-C rejected; H-D deferred post-SPEC-047.
- **OQ-1 → Lt 8/4/2 CONFIRMED** (personal/enterprise/federal floor; cosine decay; strict-improvement gate replaces no-regression in REQ-022; rejected-edit buffer adopted).
- **OQ-3 → ≥3 pytest-invocable cases** to unlock code mutation; ent/federal human-authored; personal may LLM-seed with audit-warn; per-skill sandbox timeout override threaded through (DC-5).
- **DC-5 correction ACCEPTED**: EvalRunner default impl = thin adapter over `arcskill.hub.dry_run` (no arcagent/arcrun sandbox dep). OQ-9's "SPEC-036 VmBackend" wording is superseded.

## Open questions (resolved with recommended defaults — none blocking)

- **OQ-1 [DEEPEN — PINNED] — SkillOpt change-bound method & constants.** *Resolved (see SDD §7 Research Insights).* Paper found & verified (Microsoft Research, arXiv:2605.23904, open-source). Bounding method = **edit-operation count per step (`add`/`delete`/`replace` hunks) = a "textual learning rate" `Lt`**; SkillOpt default `Lt=4`, cosine-decay schedule, floor `Lt=2`, **strict-improvement** validation gate. Pinned Arc config adds `max_edits`/`edit_schedule`/`min_edits_floor` (SkillOpt-faithful) alongside Arc's code-specific `max_files_touched`/`max_lines_changed`/`max_ast_distance` caps; per-tier table pinned in SDD §7 (federal floor `Lt=2` — **not 1**). Josh's hypothesis is **validated with a ceiling**: bounded beats unbounded (removing the budget dropped accuracy), but tightest (`Lt=1`) underperforms moderate — the convergence guarantee comes from the strict-improvement eval gate, not extreme tightness. Also adopting SkillOpt's **rejected-edit buffer** + strict-improvement golden-task gate.
- **OQ-2 [DEEPEN — RESOLVED, awaiting Josh's pick] — Hermes self-adaptation approach.** *Resolved to a menu (see SDD §4.2 Research Insights).* Hermes = **NousResearch Hermes Agent** + its `hermes-agent-self-evolution` (DSPy+GEPA), confirmed as the harness arcgateway already borrows from. Four adoption options presented (H-A GEPA trace-reflection → **adopt**; H-B autonomous Curator/sweep → **adopt sweep, defer merge**; H-C SQLite store → **reject**, conflicts with glass-box-markdown; H-D Darwinian code-evolver → **defer** to post-SPEC-047). Seam ships now as `SkillAdapter` Protocol + config-select + `NullSkillAdapter` (mirror Brain, D-1/D-2). **Josh picks from the H-A..H-D menu — none alters the locked Protocol shape.**
- **OQ-3 — Who authors golden-task suites?** *Default:* skill-creator scaffolds `evals/` at build (REQ-070); enterprise/federal require **human-authored** cases (no LLM auto-gen gate bypass); personal may seed cases via LLM with an audit-warn. Minimum suite size TBD (start ≥3 cases).
- **OQ-4 — Retire semantics.** *Default:* disable + retain lineage (D-8); revive is operator-initiated and restores from candidate-store lineage.
- **OQ-5 — Eval-less code repair at personal?** *Default:* **No** — code mutation always requires evals (D-5); only prose is eval-less-eligible at personal.
- **OQ-6 — arcskill ↔ arcllm coupling.** *Default:* injected seams, arcskill provider-free (D-3). Not a direct arcllm dependency.
- **OQ-7 — Where do usage stats live?** *Default:* `arcskill.improver` store (`workspace/skill_traces/`), independent of arcmemory; memory enrichment optional (D-11).
- **OQ-8 — Federal per-mutation approval.** *Default:* operator approves every mutation + retire/revive at federal; enterprise approves code mutations, prose auto+audit; personal auto+audit-warn (D-10, PRD §7).
- **OQ-9 [RESOLVED — DC-5] — Sandbox provider for the eval runner.** *Resolved:* the `EvalRunner` default impl is a thin adapter over **`arcskill.hub.dry_run`** (Firecracker federal / Docker fallback, tier-aware, `SandboxRequired` fail-closed) — already inside arcskill, no arcagent/arcrun sandbox dependency. The earlier "SPEC-036 VmBackend" wording is superseded (that class does not exist). Golden-task suites must be pytest-invocable. Fail-closed at ent/federal; personal degrades to host with audit-warn (D-6).

## Acceptance snapshot (full in PRD §6 / PLAN gates)

- **AC-1** improver-less by default (silent no-op, zero files). **AC-2** real run → seeded code bug → code patch → passes golden suite in sandbox → re-signed (agent DID) → re-verified via hub → reloaded → task now passes (**the producers-unwired defense**). **AC-3** no suite → code mutation blocked (audited). **AC-4** over-bound candidate rejected pre-eval (audited). **AC-5** unused skill retires, operator revives, both operator-signed. **AC-6** audit = operator key, artifact = agent DID. **AC-7** arcagent LOC down, core <3500, all packages ruff+mypy-strict clean.

## Conflicts found with existing code

1. **Duplicate improver homes.** SPEC-012 already shipped the improver in `arcagent/modules/skill_improver/` (COMPLETE). SPEC-044 **relocates + deletes** it (no-legacy). The relocation must be behavior-preserving for the retained parts (ported tests pass unchanged) — Phase 1 handles this.
2. **`SkillImproverConfig` lived under `[modules.skill_improver.config]`.** New home is `[skills.improver]` (aligns with `[skills.hub]` in arcskill). This is a config key change with no back-compat shim (no-legacy) — arcagent's module registration for the old key is deleted in the same change.
3. **Judge was the acceptance gate.** `engine.py` currently accepts on `frontier.add_if_improves` (judge delta). SPEC-044 demotes the judge to *ranking* and inserts the golden-task suite as the hard gate — a behavior change to the acceptance path (Phase 3), not a pure move.
4. **`trace_collector.py` is bus-coupled.** It cannot move wholesale to provider-free arcskill; it splits (signal extraction → `skilladapt/extension.py`; storage/analysis → arcskill). Called out in the PLAN feature-inventory.
5. **Signing already agent-DID (`engine.apply_result`), audit already WORM (`candidate_store.append_audit`).** SPEC-044 keeps agent-DID signing and makes the audit sink **operator-key** per SPEC-053 — the `actor_did="did:arc:skill-improver"` placeholder in `candidate_store._mutation_audit_event` must become operator-key-signed emission (Phase 7).
6. **[/deepen DC-5] The "SPEC-036 sandbox / VmBackend" the SDD names does not exist by that name.** The real, tier-aware, fail-closed sandboxed pytest runner is **`arcskill.hub.dry_run`** (Firecracker federal / Docker fallback, `SandboxRequired` when absent) — already inside arcskill. The `EvalRunner` seam's default production impl is a thin adapter over `hub.dry_run`, **not** an injected arcagent/arcrun sandbox (`arcrun/sandbox.py Sandbox.check()` is a policy checker, not an executor — do not wire to it). This *simplifies* the design (no cross-package sandbox dep) but golden-task suites must be **pytest-invocable** and fit the sandbox timeout. Phase 3 must implement `EvalRunner` over `hub.dry_run`; SDD §3 Research Insights (DC-5) has the correction. Flagged, not silently rewritten.

## Accepted deviations — Phases 3.3–9 (implementation, 2026-07-08)

Same class as HANDOFF deviations 1–4 (spec-vs-reality corrections, flagged not silently rewritten):

5. **Golden-task execution uses a stdlib harness, not the `pytest` binary, inside the sandbox.** The minimal, network-isolated sandbox images (`python:3.11-slim`, Firecracker rootfs) don't ship `pytest` and `network=none` forbids installing it. `HubEvalRunner` writes a tiny stdlib harness that imports each golden `test_*` function and runs it, printing one machine-readable line per case (per-case pass/fail). Golden files stay **pytest-format** (`evals/test_*.py::test_*`) — faithful to "pytest-invocable cases" — but the sandbox does not shell out to the `pytest` binary. Strictly more portable and secure (no extra sandbox dep).
6. **Mutated-skill re-verify uses the arctrust agent-DID `.arcsig` sidecar (SPEC-033), not `hub.verify_bundle` (Sigstore).** `hub.verify_bundle`/`verify_artifact_at_load` verify hub-*installed* bundles that carry a Sigstore bundle; a locally-mutated skill carries an agent-DID sidecar in arctrust's `ArtifactSignature` format. The provider-free improver re-verifies through arctrust directly (`codepatch._reverify`, fail-closed with rollback) — the correct load-gate for this artifact type, matching how the existing improver/loader verified. Same shape of correction as DC-5.
7. **`max_ast_distance` is a char-level `SequenceMatcher` distance proxy ([DEEPEN] for a true AST metric).** Used ONLY as a convergence regularizer per §8 — never a security gate (the sandbox + re-sign + re-verify chain contains a malicious patch). The primary SkillOpt bound is `max_edits` (edit-op hunks); `max_ast_distance` defaults off (0.0) at personal.
8. **Opt-in Docker `mount` added to the hub dry-run path** (`_run_docker(mount=True)` + public async `execute_in_sandbox`, per-skill `timeout_s`) so the eval harness sees the materialized bundle. The install dry-run keeps `mount=False` (unchanged). This also fixes a latent gap: the Docker dry-run previously bind-mounted nothing.

9. **[MED-4] arcmemory `insight` producer = generic Brain recall, not a failure-only channel.** `ctx.data["insight"]` is populated by a new memory-module `agent:pre_respond` hook (priority 100, runs before the skills reader at 150) that calls the active Brain's `retrieve(task)` through the memory ACL. The Brain returns generic query-conditioned recall, not a strictly *recurring-failure* abstraction; the protocol docstring is corrected to say so. A narrower failure-only insight channel (a dedicated Brain method / retrieve filter) is a possible **arcmemory / SPEC-047 follow-on**. Memory-off (NullBrain) leaves `insight` empty, so the improver stays fully memory-less (REQ-060).

## Adversarial-review closure (2026-07-08, Opus fix pass + rework per team rulings)

All findings fixed; full package matrix + `ruff` + `mypy --strict` + LOC budgets green. The first
pass used a dedicated `ProactiveEngine`, a parallel `SkillApprover`, and an `_agent_skills` filter;
the team's follow-up research rulings replaced those with the canonical mechanisms below (recorded
so the change is explicit, not silent).

| # | Finding | Fix (final) |
|---|---|---|
| **C-1** | `review_lifecycle` had no producer | A canonical `@background_task` sweep loop (`skills_review_lifecycle_loop`, mirroring memory's consolidate loop) drives `run_lifecycle_sweep` on `[modules.skills] sweep_poll_seconds` (distinct from the 30-day window). The ProactiveEngine is dormant infra (noop handler, no `add()` seam) → **deferred to SPEC-042**. AC-5 drives the loop's poll body → `review_lifecycle` → retire → reconcile → operator-signed WORM audit (`test_lifecycle_sweep_e2e.py`), no facade call (DC-10). |
| **C-2** | Operator-approval (HITL, D-10) gate absent | The improver's approval seam is a thin injected `ApprovalProvider` callable (None = deny); arcagent binds it to the **shared SPEC-035/043 `HumanGate`** (`build_skill_approval_provider`) — operator-signed grants, ASI09 self-approval guard, fail-closed at federal — NOT a parallel system. Tier ladder in the improver (federal = every transition; enterprise = code; personal = auto). ACs: `test_approval_gate.py`, `test_approval_wiring.py`, `test_lifecycle.py`. |
| **H-3** | `lifecycle_state` had no consumer | A **suppression set on `CapabilityRegistry`** (single source of truth): `suppress_skill` removes a retired skill from `_skills` so BOTH read sites — `_agent_skills` AND the prompt manifest (`format_for_prompt`) — exclude it; `register_skill` skips it on re-scan; `unsuppress_skill` restores it. The skills module reconciles from `adapter.retired_skills()` each sweep and rehydrates at `agent:ready`. AC: `test_retire_hides_offering.py` (both sites + rescan + revive). |
| **3b** *(new, merge-blocking)* | `st.skill_registry` never wired in prod | The real `CapabilityRegistry` is delivered on the existing `agent:ready` emit; `_skill_path` reads its real shape (`_skills` dict + `SkillEntry.location`), so the improver can locate skills in production (previously always `None`). The broken `_reload` no-op is dropped — the provider reads skill bodies fresh on `load()`, so a body/script mutation is live without a registry reload. Proven in `test_lifecycle_sweep_e2e.py` (`_skill_path` resolves the real location). |
| **M-4** | dead `insight` consumer read | Wired via the memory `agent:pre_respond` hook (deviation #9). ACs: `test_insight_producer.py`. |
| **M-5** | SkillOpt features unwired | `_optimize_code` passes a cosine-decayed `edit_budget` from `ChangeBound.scheduled_edits`; a bounded per-skill rejected-edit buffer feeds the mutator as negative feedback. AC: `test_skillopt_wiring.py`. |
| **L-6** | dead `max_prose_edit_distance` knob | Deleted (prose drift already bounded by `anchor_distance_threshold`). |
| **L-7** | `hub/_docker.py` hardcoded `exit_code=0` | Switched to `DockerBackend.run_separated` — the container's real exit code is captured. |
| **L-8** | audit actor hardcoded `did:arc:skill-improver` | Mutation/audit actor is the constructed agent DID (artifact side); WORM chain stays operator-signed (REQ-050 preserved). |

**Test-suite hangs (for the PR description):** `arcgateway-slack` `test_connect_raises_on_import_error` was a pre-existing unit-test hang (built its `sys.modules` removal set from already-imported modules → empty when run first → `patch.dict` no-op → `connect()` did live Socket Mode network I/O) — **fixed here** (patch a fixed slack_bolt module list to `None`). `arcui` `test_browser_message_reaches_echo_executor` (chat-WS echo) is a separate pre-existing hang under investigation in another session — **not touched**, hermetically unrelated to this diff.

## Learnings

- **Producers-unwired defense held.** Every mechanism (P3.3 runner, P4 code-repair, P5 change-bound, P6 sweep, P7 audit-split) has an E2E/facade test driving it through the real `SkillAdapter` surface (`observe`/`on_turn_end`/`maybe_improve`/`review_lifecycle`) — never a direct `engine.optimize` call. AC-2 runs the full chain in a **real Docker sandbox** with real arctrust agent-DID sign/verify.
- **The LLM/Mutator is always an injected seam.** Injecting a deterministic fake Mutator is not a rigged fixture — it is the standard provider boundary; the sandbox, sign, verify, reload, and audit links stay real.
- **Coverage:** `arcskill.improver` package 91.6% line / 85.9% branch; `arcagent.skilladapt` 90%. AC-7: arcagent source NCLOC 31,119 (down ~2,430 from develop's 33,549); core 3,498/3,500.

## Next steps

`/deepen SPEC-044` (pin OQ-1 SkillOpt bounds + OQ-2 Hermes options) → `/implement SPEC-044` (Phase-0 scaffold + feature inventory first). **DONE — all phases 0–9 landed; AC-1..7 green.**
