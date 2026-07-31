# SPEC-044 — Best skill builder/improver — SDD

**Status:** COMPLETE
**Traces:** PRD REQ-001..REQ-070, NFR-001..006
**Coding identity:** principled-coder (Simplicity → Modularity → Security → Scalability)

---

## 1. Design in one paragraph

Extract the skill-improvement **logic** out of `arcagent/modules/skill_improver/` into **`arcskill.improver`** — a subpackage of the optional **arcskill** package, beside `hub/`. This mirrors SPEC-041 exactly: just as `arcmemory` is a separate installable package (not folded into arcagent) that arcagent activates through a `Brain` seam, **arcskill** is the separate installable package that arcagent activates through a `SkillAdapter` seam. Josh's ruling (2026-07-08): arcagent already manages skills (writes, loads, runs) on its own; **arcskill IS the optional supercharger** that adds adaptation, improvement, and skill management. `arcskill.improver` is the toggleable **self-improvement layer** that drives sibling `hub`'s `dry_run` sandbox + verify gate and uses arctrust (sign/audit), but imports no `arcagent`/`arcllm`/`arcmemory`. **A deployment that never installs arcskill has skills that write, load, and run — and never self-modify; installing arcskill still activates improvement only when config enables it.** arcagent keeps only a thin, config-enabled **extension**: a structural **`SkillAdapter` Protocol** with a no-op **`NullSkillAdapter`** default, config-selected (`none` / `arcskill` / signed BYO). The extension's module-bus hooks observe skill usage and forward **primitive** signals into the adapter; the proactive engine schedules the improvement pass, which runs **behind the scenes** — the operator sees better skills, not a running optimizer. Inside `arcskill.improver`, **one integrated method** (not two systems) expands mutation from prose-only to **prose + code patch** over the whole skill bundle: a **GEPA-style trace-reflection** proposal step ("why did this skill fail?") feeds a **SkillOpt-bounded** edit, gated by a **deterministic strict-improvement golden-task eval suite** run in the arcskill `dry_run` sandbox (LLM-judge only ranks), bounded per step by first-class **change-bound config** layered on the existing guardrails, driving a **nudge → usage → retire** lifecycle state machine. Every mutation is re-signed (agent DID) and re-verified through the hub gate; every transition emits an **operator-signed** WORM audit event. All LLM/eval/audit/signing dependencies enter `arcskill.improver` through injected Protocol seams, so it imports no `arcagent`/`arcllm`/`arcmemory` and stays arch-test-green.

---

## 2. Module boundaries (the load-bearing decision)

| Concern | Owner | Does | Must NOT do |
|---|---|---|---|
| **Skill-improvement logic** — trace store, prose+code mutation, golden-task eval gate, guardrails+change-bound, candidate store/lineage, lifecycle state machine, audit-event construction | **`arcskill.improver`** (subpackage of the optional arcskill package, beside `hub/`) | Everything skill-improvement. Pure logic over injected seams. May use sibling **`arcskill.hub`** (sandbox/verify/bundle model) + **arctrust** (sign/audit). | Import arcagent/arcllm/arcmemory; subscribe to the module bus; call an LLM directly; schedule itself |
| **Wire + schedule + select** | **`arcagent.skilladapt`** (NEW, thin — mirrors `arcagent/brain/`) | `SkillAdapter` Protocol + `NullSkillAdapter` + `select`; module-bus hooks forward primitive signals; proactive engine triggers the pass; injects arcllm Mutator/Judge, sandboxed EvalRunner, operator AuditSink, agent-DID Signer | Contain improvement *logic* — talks to the improver only through the Protocol |
| **Generate prose/patch text; judge** | **`arcllm`** (via injected `Mutator`/`Judge` seam) | Bounded structured completion for mutation + LLM-as-judge scoring | Persist, gate, sign, or decide acceptance |
| **Run golden-task suite / validate patch** | **Sandbox = `arcskill.hub.dry_run`** (Firecracker/Docker; via injected `EvalRunner` seam — SPEC-036 lineage, DC-5) | Execute untrusted skill code + eval cases in isolation; return pass/fail + scores | Rank, mutate, or persist |
| **Sign / verify bundle** | **`arctrust`** + `arcagent.capabilities.artifact_signing` (via injected `Signer`) | Ed25519 sidecar sign (agent DID) + re-verify at load | Decide *what* to sign |
| **Audit** | **`arctrust` WORM** signed by **operator key** (SPEC-053, via injected `AuditSink`) | Tamper-evident, operator-signed lifecycle trail | Be signed by the agent DID |
| **Optional failure-abstraction enrichment** | **arcmemory** `Brain.retrieve` insight channel (SPEC-041) | Supply recurring-failure insight text | Be required; be imported by arcskill |
| **Hub load gate** | **`arcskill.hub`** (existing) | Re-verify + scan the mutated bundle before reload | — |

**Subtle lines.** *Accruing usage is not improving* — the value is the gated, bounded, signed **mutation**. *The improver never executes code itself* — it hands candidates to the sandboxed `EvalRunner`. *arcskill calls no LLM directly* — the `Mutator`/`Judge` seams are injected by arcagent from arcllm, exactly as arcmemory injects `Distiller`. *arcagent holds no improvement logic* — only the `SkillAdapter` seam, which is what makes skill self-adaptation swappable and, per SPEC-047, a first-class extension point. *Audit authority ≠ audited subject* — operator key signs the trail; the agent DID only signs the artifact it produced (SPEC-053).

---

## 3. Dependency DAG placement

```
arctrust  (leaf: KeyPair, sign/verify_artifact, AuditEvent/AuditSink/WORM, OperatorKey)
   ▲              ▲                      ▲
   │              │                      │
arcskill        arcllm (embed/complete)     arcmemory (Brain, optional)
   ▲  (hub: install/verify/lock/scan + dry_run sandbox)
   │
arcskill.improver   (subpackage of optional arcskill — pure logic; uses hub+arctrust;
   ▲          imports no arcagent/arcllm/arcmemory; seams: Mutator/Judge/EvalRunner/Signer/AuditSink)
   │  (satisfies SkillAdapter structurally)
arcagent.skilladapt  ── injects arcllm-backed Mutator/Judge, arcskill-dry_run EvalRunner,
   │                     operator AuditSink, agent-DID Signer; forwards Brain insight text
arcagent.core (bus, hooks, proactive engine, config)
```

`arcskill.improver` uses sibling `arcskill.hub` (dry_run sandbox + verify gate + bundle model) and `arctrust` (sign/audit). It does **not** depend on arcllm/arcagent/arcmemory. arcagent references `arcskill` only through config-select — a **lazy import inside `skilladapt/select.py`, exactly like `arcmemory`** (no static arcagent→arcskill dependency). `pip install arcagent` **without** `arcskill` → skills write/load/run and self-improvement is a silent `NullSkillAdapter` no-op (AC-1); install `arcskill` + set `[skills.improver] adapter = "arcskill"` → self-improvement activates (arcskill installed but adapter unset = still a no-op).

### Research Insights — In-repo seam verification (DC-1..DC-10, producers-unwired defense)

Every integration point the SDD names was checked against the code at the signature/shape level ([[feedback_producers_unwired_pattern]]). Nine of ten seams are confirmed; **one (DC-5, the sandbox) is BROKEN as described and must be corrected before Phase 3.**

| DC | Seam claimed by SDD | Reality in code | Verdict |
|---|---|---|---|
| **DC-1** | `arcagent/brain/select.py` config-select + BYO signing gate to mirror | `select_brain(setting, …, brain_allowlist)`: `none`→`NullBrain`; `arcmemory`/`auto`→lazy `importlib.import_module("arcmemory")` (no static dep); dotted path→`_load_custom` refused unless in `brain_allowlist` above personal, **fail-closed, never imported**. Exactly the pattern to clone for `skilladapt/select.py`. | ✅ confirmed |
| **DC-2** | Bus hooks `agent:post_tool`, `agent:post_plan`, `agent:pre_respond` | `agent:post_tool` **emitted** at `core/tool_registry.py:504`; `agent:pre_respond` **emitted** at `core/agent_dispatch.py:111`; `agent:post_plan` **emitted** via the arcrun→bus bridge `create_arcrun_bridge` (`core/model_manager.py:143-160`, maps arcrun `turn.end`→`agent:post_plan` and schedules `bus.emit`). All three are live *producers*, not just subscription points. | ✅ confirmed (post_plan is bridge-produced from arcrun `turn.end` — verify the bridge is installed in the E2E test, not just the hook) |
| **DC-3** | `arctrust.sign_artifact` / `verify_artifact` | Present: `arctrust/artifact.py:62 sign_artifact`, `:77 verify_artifact`; low-level `keypair.sign/verify`; `Signer` protocol in `signer.py`. | ✅ confirmed |
| **DC-4** | agent-DID sidecar signer = `arcagent.capabilities.artifact_signing` | Exists (`capabilities/artifact_signing.py`) and is **already used** by today's improver: `engine.py:15` imports it, `apply_result` (`engine.py:216`) signs the mutated skill with `signer_did`+`signing_key` (SPEC-033). SDD's `Signer.sign_bundle` extends this from single-file to whole-bundle — an expansion, not a gap. | ✅ confirmed |
| **DC-5** | "SPEC-036 sandbox / VmBackend" injected as `EvalRunner` (SDD §2/§4, OQ-9) | **No class named `VmBackend`; no literal `SPEC-036` tag in code.** The real, *tier-aware, fail-closed* sandboxed test runner is **`arcskill.hub.dry_run.dry_run()`** (public), which selects `FirecrackerSandbox.execute(skill_path, test_command="pytest", timeout_s=10) -> DryRunResult` (`hub/_firecracker.py:190`, federal) or the Docker backend (`hub/_docker.py`), raising `SandboxRequired` when unavailable. `arcrun/sandbox.py Sandbox.check()` is a **policy checker, not an executor** — do NOT wire the EvalRunner to it. | ⚠️ **BROKEN as named / BETTER in reality** — see correction below |
| **DC-6** | operator-key WORM audit (SPEC-053) as injected `AuditSink` | `arctrust/audit.py WormSink.__init__(signer: Signer)` signs each event (`self._signer.sign(...)`, `audit.py:280`); `AuditSink` Protocol + `emit(event, sink)` exist. arcagent builds the operator signer via `OperatorKey.load(...).into_signer(algorithm)` (`core/agent.py:215-222`, `arctrust/operator.py:90`). Today's improver `candidate_store._mutation_audit_event` hardcodes `actor_did="did:arc:skill-improver"` and emits into whatever sink it's handed — so the **seam exists; the gap is wiring the operator-key `WormSink` in as the injected `AuditSink`** and dropping the placeholder actor. Matches README conflict #5; Phase 7 work. | ✅ confirmed (wiring gap, not a missing seam) |
| **DC-7** | arcmemory insight enrichment consumed as primitive text (REQ-060) | `Brain.retrieve(query, *, summary="", cues=…) -> str` (`brain/protocol.py:45`) returns injectable text; `arcmemory` mints insights during `consolidate` (`arcmemory/consolidate.py`, `InsightStore`) and surfaces them through `retrieve`. So the improver gets insight text by calling `Brain.retrieve(...)` and passing the returned `str` as the `insight` primitive to `Mutator.propose`. **Caveat:** there is no insight-*only* retrieval API — `retrieve` returns blended recall text, not a pure insight channel. Adequate for REQ-060 (primitive text), but don't design as if a dedicated insight feed exists. | ✅ confirmed (with caveat) |
| **DC-8** | `arcskill.hub` re-verify/scan load gate for the re-signed bundle | `hub/verify.py`: `verify_artifact_at_load` (`:272`), `verify_bundle` (`:181`), `_stage_verify_signature` (installer), `trust_backend.py` calls `verify_artifact_at_load`. The re-sign→hub-re-verify→reload chain (REQ-012) has a real load gate to route through. | ✅ confirmed |
| **DC-9** | Existing `modules/skill_improver/` tree = ~3,150 LOC to relocate/delete | `wc -l` totals **3,149** across the 17 files; matches the PLAN feature-inventory line-for-line (models 271, engine 266, evaluator 202, candidate_store 193, guardrails 121, pareto 123, reflector 133, config 48, trace_collector 281, capabilities 290, skill_improver_module 399, _runtime 179, nudge/* 634). | ✅ confirmed |
| **DC-10** | proactive engine schedules `review_lifecycle` | `modules/proactive/engine.py` exists and emits its own events (`:278`); the existing improver already binds via `@hook`. The extension can schedule the lifecycle sweep off the proactive tick as claimed. | ✅ confirmed (validate the tick actually fires the sweep in the AC-5 E2E, not a direct `lifecycle.sweep()` call) |

**DC-5 correction (load-bearing — flag for Josh, do not silently rewrite the design).** The SDD/README (§2 boundary table "SPEC-036 sandbox", §4.1 `EvalRunner` "SPEC-036 sandbox behind this", OQ-9 "reuse SPEC-036 VmBackend/Firecracker") name a sandbox class that does not exist and imply it is injected *from arcagent/arcrun*. The correct and **simpler** reality: the sandboxed pytest runner **already lives inside arcskill** as `arcskill.hub.dry_run` (Firecracker federal / Docker fallback, tier-aware, `SandboxRequired` fail-closed). Implications:
- The `EvalRunner` Protocol seam stays (for deterministic test fakes), but its **default production implementation is a thin arcskill adapter over `hub.dry_run`** — **no arcagent/arcrun sandbox dependency, no new sandbox to build.** This *strengthens* modularity (arcskill executes its own skills in its own sandbox) and removes a cross-package coupling the SDD assumed.
- Golden-task suites must be **pytest-invocable** because `dry_run`/`FirecrackerSandbox.execute` runs `test_command="pytest"`. `EvalRunner.run(view, cases)` maps `EvalCase`s → a pytest run inside the sandbox and parses `DryRunResult` (exit code + captured stdout/stderr) into `EvalOutcome`s. Note the current `timeout_s=10` default — golden suites must fit it or the seam must thread a per-skill timeout.
- Fail-closed at ent/federal (REQ-023) is *already* the `dry_run` contract (`SandboxRequired` raised when Firecracker/Docker absent) — reuse it rather than reinventing the gate.

Recommend the implementer either (a) promote a small public `arcskill.hub` "run tests in sandbox" entry (it's already public via `dry_run`) that `EvalRunner` wraps, or (b) have `arcskill.improver`'s default `EvalRunner` call `hub.dry_run` directly (same package — no seam violation). Do **not** reach into arcrun for this.

---

## 4. Components

### 4.1 `arcskill.improver` (new subpackage of arcskill)

Relocated + expanded from `arcagent/modules/skill_improver/`. Files:

| File | Origin | Role |
|---|---|---|
| `models.py` | moved from arcagent | `SkillTrace`, `ToolCallRecord`, `Candidate`, `BundlePatch` (NEW), `EvalCase`/`EvalOutcome` (NEW), `MutationEvent`, `LifecycleEvent` (NEW), `OptimizeResult` |
| `config.py` | moved + extended | `ImproverConfig` — existing fields + **`change_bound`** block (REQ-030) + lifecycle thresholds |
| `guardrails.py` | moved + extended | existing checks + **`ChangeBound.check(candidate, seed, tier, skill_override)`** (REQ-030/031) |
| `evalgate.py` | NEW | Golden-task suite loader + regression gate; the **hard gate** (REQ-020/022) |
| `mutate.py` | replaces `reflector.py` + adds code path | Prose mutation (existing) + **code-patch generation** over the bundle (REQ-010/011) via injected `Mutator` |
| `engine.py` | moved + rewired | Orchestrates: analyze failures → propose (prose|code) → change-bound → **eval-gate** → judge-rank → apply. Judge is ranking-only now |
| `candidate_store.py` | moved | lineage/rollback/audit (REQ-051/052) — audit via injected operator `AuditSink` |
| `pareto.py` | moved | frontier ranking (judge scores only rank; eval-gate is separate) |
| `lifecycle.py` | NEW | State machine `active/nudged/improving/underperforming/retired/revived` + transition audit (REQ-041..045) |
| `nudge.py` | moved from `nudge/` | create-nudge (advisory) + **improve-nudge** (NEW, REQ-042) |
| `seams.py` | NEW | `Mutator`, `Judge`, `EvalRunner`, `Signer`, `AuditSink` Protocols (injected; REQ-004) |
| `improver.py` | NEW | `ArcSkillImprover` — the `SkillAdapter`-shaped facade arcagent wires |

**Seams (Protocols in `seams.py`)** — arcskill declares, arcagent injects:

```python
class Mutator(Protocol):
    async def propose(self, *, kind: str, current: BundleView, failures: str,
                      insight: str, bound: ChangeBound) -> BundlePatch | None: ...

class Judge(Protocol):
    async def score(self, view: BundleView, cases: list[EvalCase]) -> dict[str, float]: ...

class EvalRunner(Protocol):          # SPEC-036 sandbox behind this
    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]: ...

class Signer(Protocol):              # agent-DID sidecar (SPEC-033)
    def sign_bundle(self, files: Mapping[Path, bytes]) -> None: ...

class AuditSink(Protocol):           # operator-key WORM (SPEC-053)
    def emit(self, event: MutationEvent | LifecycleEvent) -> None: ...
```

`BundleView` is a read model of a skill bundle (SKILL.md text + script file bytes + frontmatter + tags + eval cases). `BundlePatch` is a validated multi-file diff (files touched, per-file new bytes, change metrics). Both are arcskill-owned value types — no arcagent import.

### 4.2 `arcagent.skilladapt` (new, thin — mirrors `arcagent/brain/`)

| File | Role |
|---|---|
| `protocol.py` | `SkillAdapter` Protocol (structural, primitives only) + `NullSkillAdapter` no-op default (REQ-002) |
| `select.py` | config → `NullSkillAdapter` / `arcskill.improver.ArcSkillImprover` (lazy import) / signed BYO class-path (REQ-003, BYO signing gate) |
| `extension.py` | module-bus hooks (`agent:post_tool`, `agent:post_plan`, `agent:pre_respond`) forwarding primitive signals; proactive-engine schedule for the improvement pass; wires the injected seams |

`SkillAdapter` Protocol (primitives at the boundary, so arcskill need not import arcagent):

```python
@runtime_checkable
class SkillAdapter(Protocol):
    async def observe(self, *, skill_name: str, tool_name: str, status: str,
                      error_type: str | None, session_id: str | None = None) -> None: ...
    async def on_turn_end(self, *, turn: int, outcome: str, session_id: str | None = None) -> None: ...
    async def maybe_improve(self, *, insight: str = "", session_id: str | None = None) -> None: ...
    async def review_lifecycle(self, *, turn: int) -> None: ...   # retire/revive sweep
```

`NullSkillAdapter` returns immediately from every method → **no traces, no files, no mutations** when improvement is off (REQ-002, AC-1).

**Deletion (no-legacy, REQ-001).** The entire `arcagent/modules/skill_improver/` tree (engine, reflector, evaluator, pareto, guardrails, candidate_store, models, `_runtime`, capabilities, `skill_improver_module`, `trace_collector`, `nudge/`) is **deleted in the same change** that lands `arcskill.improver` + `arcagent.skilladapt`. Trace *signal extraction* (formerly `trace_collector`'s bus coupling) collapses into `skilladapt/extension.py`; trace *storage/analysis* moves to arcskill. Net arcagent LOC **down** (NFR-001).

### Research Insights — Hermes self-adaptation options (OQ-2 — present, don't decide)

*Hermes identified and verified.* The `Hermes` the arcgateway package references is **NousResearch Hermes Agent** (`github.com/nousresearch/hermes-agent`) — Arc already lifted its gateway patterns (`arcgateway/pairing.py` "Lifted from Hermes `gateway/pairing.py`", the session.py PR #4926 race guard, reconnect backoff, `.clean_shutdown` marker). Its self-improvement lives in a companion repo, **`NousResearch/hermes-agent-self-evolution`** ("optimize skills, prompts, and code using DSPy + GEPA").

**How Hermes self-adapts (verified from its docs/repo):**
- **Procedural-memory skill loop.** After each completed task the agent writes a reusable `SKILL.md` into **SQLite**; successful approaches become skills that persist across sessions. It can `create / update / delete` its own skills via a `skill_manage` tool. "If a better approach consistently outperforms the stored one, the skill is revised."
- **GEPA (Genetic-Pareto) trace-driven evolution.** Reads execution traces to understand *why* a run failed (not just that it did), then proposes **targeted** mutations — API-only, no GPU, "~$2–10 per optimization run." DSPy powers the reflective prompt evolution.
- **Autonomous Curator (v0.12.0+).** A background process **grades, consolidates, and prunes** the skill library on a **7-day cycle** — i.e. Hermes already runs the "retire/consolidate" half of a lifecycle.
- **Five mandatory constraint gates on every evolved variant:** (1) full pytest suite passes 100%; (2) size limits (skills ≤15 KB, tool descriptions ≤500 chars); (3) caching compatibility preserved; (4) semantic preservation of original purpose; (5) **human review via PR — never a direct commit.** Roadmap phases: SKILL.md → tool descriptions → system-prompt sections → **tool implementation code** (a "Darwinian Evolver") → continuous pipeline.

**Where Arc already matches or exceeds Hermes:** deterministic golden-task gate (Hermes gate #1 = pytest, same idea; Arc adds strict-improvement + sandbox isolation), operator-signed WORM audit (Hermes has none — PR review is its only control), agent-DID re-sign + hub re-verify (Hermes has no artifact signing), change-bound config (Hermes has only static size caps 15 KB / 500 chars). Hermes' GEPA "code phase" is still roadmap; Arc's REQ-010 code-repair ships it gated.

**Adoption options for `arcskill.improver` + `SkillAdapter` (Josh picks — each is additive to the locked D-1..D-12 seam):**

| Option | What Hermes does | Adopt in Arc as | LOC / complexity | Security posture per tier | Recommendation |
|---|---|---|---|---|---|
| **H-A: GEPA-style "why-it-failed" trace reflection** | GEPA reads traces to explain *why* a run failed, then proposes targeted edits | Add a `reflect(failures) -> str` step feeding `Mutator.propose(failures=...)` — the improver already has `error_type` per trace; enrich into a causal summary before mutation. Composes with arcmemory insight (REQ-060) as a fallback when no Brain is present. | ~40–80 LOC in `mutate.py`; one extra bounded LLM call behind the `Mutator` seam | Neutral — text-only, runs behind the injected seam; no new execution surface. Same at all tiers. | **Adopt.** Highest-leverage, cheap, directly attacks REQ-011 (turn `error_type` into a code-repair signal). Mirrors SkillOpt's rejected-edit buffer. |
| **H-B: Autonomous Curator (grade/consolidate/prune on a cycle)** | 7-day background Curator grades + prunes the skill library | Map onto `lifecycle.sweep()` (REQ-043) driven by the proactive engine tick — Arc already plans retire/revive; add periodic **consolidate** (merge redundant skills) as a lifecycle action. | ~60–120 LOC in `lifecycle.py` (+ a "consolidate" edge) | Consolidation *merges* bundles = a mutation → must pass golden-task gate + change-bound + sandbox + operator approval (federal). Fail-closed like any mutation. | **Adopt the sweep now; defer "consolidate/merge" to a follow-up.** Retire/revive is in scope (REQ-043/044); merge-consolidation adds cross-skill blast radius — nice-to-have, not headline. |
| **H-C: SQLite-backed skill store** | Skills persist in SQLite | *Reject for storage.* Arc's glass-box markdown + `manifest.json` under `workspace/skill_traces/` (SDD §6) is the deliberate federal choice (auditable, diffable, signable sidecars). SQLite would hide the artifact from `verify_artifact`/audit. | n/a | SQLite blob is opaque to sidecar signing + WORM diff — a *regression* federally. | **Reject.** Conflicts with D-8/§6 and the arcmemory "glass-box markdown is truth" locked decision. Note only. |
| **H-D: GEPA "Darwinian Evolver" for tool-implementation code** | Roadmap phase: evolve tool *implementation* code via population + selection | Arc's REQ-010 code-repair is the *targeted single-patch* version of this. A population/Pareto variant already exists (`pareto.py`) — could widen from prose-ranking to code-variant tournaments. | High — multi-candidate code eval per step multiplies sandbox runs (Scalability/NFR-005 cost) | Each variant is a full sandboxed eval — bounded concurrency required; cost ceiling per SkillOpt rollout-batch guidance. | **Defer.** Ship single-patch code-repair (REQ-010) first; population-based code evolution is a SPEC-047-era enhancement once the seam is generalized. |

Net: **adopt H-A now** (folds cleanly into `mutate.py` and matches SkillOpt's rejected-edit-buffer finding), **adopt the H-B sweep** (already REQ-043) while deferring merge-consolidation, **reject H-C**, **defer H-D**. None of these changes the locked `SkillAdapter` Protocol shape — they are improver-internal, so the SPEC-047 generalization is unaffected.

---

## 5. Key flows

### 5.1 Observe → improve (the real production path — AC-2)

```
agent runs a skill (real turn)
  └─ tool call errors  ──hook: agent:post_tool──► extension.observe(skill, tool, "error", error_type)
                                                     └─► adapter → improver.trace_store.append(...)
  └─ turn ends         ──hook: agent:post_plan──►  extension.on_turn_end(turn, outcome)
                                                     └─► improver: usage stats += ; close span
  └─ pre-respond       ──hook: agent:pre_respond─► extension.maybe_improve(insight?)     [usage ≥ threshold]
        (optional Brain.retrieve → insight text passed in as primitive; empty if memory-less)
        └─ spawn_background(bounded, caught+audited):
            improver.engine.optimize(skill):
              1. load failing traces (buffer-aged) + BundleView
              2. eligibility: guardrails.check_eligible (min traces, cooloff, exempt tag, generation cap)
              3. Mutator.propose(kind=code|prose, failures, insight, bound)  → BundlePatch
              4. guardrails + ChangeBound.check(patch, seed, tier, skill_override)   ─ reject→audit, stop
              5. EvalRunner.run(patched view, golden cases)  [SPEC-036 sandbox]
                   ─ no suite? fail-closed per tier (REQ-021)  ─ regression? reject→audit
              6. Judge.score(...) → rank in Pareto frontier (ranking only)
              7. federal? require operator approval (SPEC-043 ladder) before apply
              8. apply: write patch → Signer.sign_bundle (agent DID) → hub re-verify
                        → candidate_store.save(lineage) → AuditSink.emit(operator-signed)
                        → skill registry re-discover → reload
```

Every numbered step has an E2E acceptance test that drives it through the **real** extension+hook path, not a rigged optimizer call (producers-unwired defense — PLAN §Phase gates).

### 5.2 Lifecycle sweep → retire/revive (AC-5)

```
proactive engine tick ──► extension.review_lifecycle(turn)
   └─► improver.lifecycle.sweep():
         for each skill:
           unused > inactivity_window            → RETIRE  (disable, keep lineage)   → audit
           failure_rate < floor after N attempts → RETIRE                            → audit
         (revive is operator-initiated: lifecycle.revive(skill) → restore from lineage → audit)
   federal: RETIRE/REVIVE require operator approval before commit
```

State machine (each edge = an audited `LifecycleEvent`):

```
        nudge-improve            apply (pass)
active ───────────────► improving ───────────► active
  │  ▲                     │ reject/exhausted
  │  │ revive (operator)   ▼
  │  └──────────── retired ◄──── underperforming / unused
  └───────────────────────► retired (unused)
```

---

## 6. Data & storage

- **Trace + candidate store**: `workspace/skill_traces/<skill>/` (relocated unchanged in shape) — seed snapshot, `candidates/<id>.md` + script blobs, `manifest.json` (frontier + lineage + active id + **lifecycle_state**), path-validated (existing `_SAFE_NAME_RE`). Owned by arcskill; independent of arcmemory.
- **Golden-task suite**: `evals/` inside each skill bundle (skill-creator already scaffolds it). `EvalCase` = deterministic input + assertion; `EvalOutcome` = pass/fail + captured signal. Human-authored at enterprise/federal (no LLM auto-gen bypass; REQ-021).
- **Bundle signature**: `<file>.arcsig` sidecar per bundle file (agent DID) — existing convention (REQ-012/016).
- **Audit**: operator-signed WORM chain, stored outside the workspace (SPEC-053), separate from mutable skill code (AU-9(2)).

## 7. Config surface (`ImproverConfig`, `[skills.improver]`)

Existing fields retained (min_traces, trace_buffer_turns, optimize_after_uses, max_iterations, stagnation_limit, eval_dimensions, max_token_ratio, max_generations, anchor_distance_threshold, oscillation_distance_threshold, cooloff_turns, exempt_tags). **Added:**

```toml
[skills.improver]
enabled = false                      # NullSkillAdapter unless true (REQ-002)
adapter = "arcskill"                # none | arcskill | "pkg.mod:Class" (BYO, signed) (REQ-003)

[skills.improver.change_bound]       # SkillOpt bounded step (REQ-030/031) [DEEPEN pins values]
max_files_touched = 1                # federal floor
max_lines_changed = 40               # federal floor tighter (e.g. 15) [DEEPEN]
max_ast_distance = 0.0               # code AST edit distance ceiling (0 = disabled until DEEPEN)
max_prose_edit_distance = 0.15       # aligns with anchor_distance_threshold

[skills.improver.lifecycle]
inactivity_window_turns = 2000       # unused → retire (REQ-043)
failure_floor = 0.5                  # success-rate floor
improve_attempts_before_retire = 3
```

Change-bound resolution: `effective = min(tier_ceiling, skill_override)`, and at federal the tier floor is non-relaxable (REQ-031, §7 tier table). Per-skill override lives in the skill's frontmatter (`improver: { max_lines_changed: ... }`) validated against the tier ceiling.

### Research Insights — SkillOpt change-bound (OQ-1 PINNED)

*Source: Microsoft Research, "SkillOpt: Executive Strategy for Self-Evolving Agent Skills," arXiv:2605.23904 + `github.com/microsoft/SkillOpt` + `microsoft.github.io/SkillOpt`. Paper found and verified — real, open-source, current.*

**What SkillOpt actually does (the bounding method).** SkillOpt treats a single skill markdown document as a *trainable parameter* over a frozen LLM. A separate optimizer model turns scored rollouts into **bounded `add`/`delete`/`replace` edits** on that document. The bound is an **edit budget `Lt` = the number of edit operations allowed per optimization step**, which the paper explicitly calls a *"textual learning rate"* — "an edit budget functions as a textual learning rate, preventing useful rules from being overwritten by broad rewrites." This is exactly the SkillOpt-faithful primary bound to adopt: **count edit operations, not lines/tokens.**

**Constants / schedules (from the paper's methods + ablation tables):**
- **`Lt = 4` edits/step (default).** Ablation swept `Lt ∈ {1, 2, 4, 8, 16}`; the paper's own guidance is *"moderate learning rates (≈4–16 edits/step) beat very high/low."*
- **Cosine schedule (default), floor `Lt = 2`.** Options: `constant | linear | cosine | autonomous`. Cosine "starts with larger edits and decays toward smaller consolidation steps" and "tends to beat constant." The scheduler never drops below **2 edits**.
- **Epochs = 4** (skills "converge in ~2–4 epochs"); **rollout batch = 40/step**; **reflection minibatch = 8**; **max analyst rounds = 3**; **rewrite_max_completion_tokens = 64000**.
- **Validation gate = strict improvement.** "A candidate skill is accepted only when its selection-split score is *strictly greater than* the current selection score, so ties are rejected and the deployed skill never silently drifts." (`candidate > current → accept; candidate > best → new best`.)
- **Rejected-edit buffer:** epoch-local; retains failure patterns + the rejected edits + the score drop they caused, and *feeds them back to the optimizer next iteration* so it stops re-proposing losing edits.
- **Epoch-wise slow/meta update:** at epoch end, 20 sampled tasks compare prev-vs-current skill; removing both slow-update + meta-skill dropped SpreadsheetBench −22.5 pts — i.e. the temporal-stability mechanisms matter as much as the per-step budget.

**Josh's hypothesis ("tighter bounds → better convergence + quality") — VALIDATED WITH A CEILING, NOT MONOTONE.** Evidence *for* bounding: Table 3 shows removing the edit budget entirely ("without lr") drops accuracy **87.1/77.5/61.3 → 84.6/75.7/57.3** — bounded beats unbounded. But evidence *against tightest-is-best*: `Lt=1` underperformed `Lt=8` on LiveMath (**56.5 vs 66.9**, ~10 pts). The convergence/no-drift *guarantee* comes from the **strict-improvement validation gate**, not from an extreme budget. Takeaway for Arc: the federal floor should be **`Lt=2` (SkillOpt's own cosine floor), NOT `Lt=1`** — clamping too hard costs result quality without buying safety, because safety already comes from the golden-task gate + sandbox.

**PINNED OQ-1 config schema** (layered on existing `anchor_distance_threshold=0.15` / token-budget / oscillation guardrails — all retained per REQ-033):

```toml
[skills.improver.change_bound]
# SkillOpt-faithful PRIMARY bound — edit operations (add/delete/replace hunks) per step = "textual learning rate" Lt.
max_edits        = 4          # SkillOpt Lt default (personal shown; per-tier below)
edit_schedule    = "cosine"   # cosine | constant | linear — cosine decays start→floor, beats constant
min_edits_floor  = 2          # SkillOpt cosine LR floor; never scheduled below this

# Arc code-specific SECONDARY caps — SkillOpt is prose-only; Arc mutates code + multi-file, so it must add these.
max_files_touched     = 1
max_lines_changed     = 40    # personal shown; per-tier below
max_ast_distance      = 0.0   # REGULARIZER, NOT a security gate (see §8 Research Insights); 0.0 = disabled
max_prose_edit_distance = 0.15  # aligns with anchor_distance_threshold
```

**PINNED per-tier defaults** (`effective = min(tier_ceiling, skill_override)`; federal floor non-relaxable — REQ-031):

| Setting | Personal | Enterprise | Federal (floor, non-relaxable) |
|---|---|---|---|
| `max_edits` (Lt) | 8 | 4 | **2** *(SkillOpt floor; NOT 1)* |
| `edit_schedule` | constant | cosine | **cosine** |
| `min_edits_floor` | 2 | 2 | **2** |
| `max_files_touched` | 3 | 2 | **1** |
| `max_lines_changed` | 80 | 40 | **15** |
| `max_ast_distance` (regularizer) | 0.0 (off) | 0.30 | **0.20** |
| `max_prose_edit_distance` | 0.25 | 0.15 | **0.15** |
| Acceptance gate | golden-task **strict-improve** (`>`, no ties, zero regression) — **all tiers** | | |

**Two SkillOpt mechanisms worth adopting beyond the budget (cheap, high-leverage):**
1. **Strict-improvement eval gate (REQ-022 refinement).** Make the golden-task gate *strict*: a candidate is accepted only if it makes **≥1 previously-failing golden case pass AND regresses none** — the deterministic analogue of SkillOpt's "strictly greater, ties rejected." Prevents neutral churn/drift. Current SDD says "no regression"; tighten to "strict improvement."
2. **Rejected-edit buffer.** Arc's `candidate_store` already retains lineage + scores (REQ-051); expose *rejected candidates + their eval-fail reason* to `Mutator.propose(..., failures=...)` on the next attempt so the mutator stops re-proposing losing patches. Near-zero cost, and it is the mechanism SkillOpt credits for convergence.
3. **Cosine decay over the retire budget.** Run the edit schedule across `improve_attempts_before_retire` (start `Lt=max_edits`, decay to `min_edits_floor`) so early attempts explore and late attempts consolidate before retirement.

## 8. Security design (threat mapping)

| Threat | Mitigation |
|---|---|
| **ASI05 / LLM05 RCE** — improver executes model-authored code | Patches are inert `BundlePatch` values; execution only via the sandboxed `EvalRunner` (SPEC-036), fail-closed at ent/federal (REQ-023) |
| **ASI04 supply chain** — mutated skill is a new artifact | Re-sign (agent DID) + re-verify + hub scan before reload (REQ-012/016) |
| **ASI06 memory/context poisoning** — mutation degrades a skill | Golden-task regression gate (hard) + change-bound + guardrails; reversible via lineage (REQ-022/030/051) |
| **ASI01 goal hijack** — improver edits identity/policy | Improver only touches skill bundles under `skill_traces`/skill roots; identity.md/policy.md stay inode-locked (SPEC-035) |
| **Audit forgery** (SPEC-053) — audited subject signs its own trail | Operator key signs audit; agent DID signs only the artifact (REQ-050); sandbox/bash confined off operator key + `.audit/**` (SPEC-035) |
| **BYO adapter RCE** (SPEC-041 review) | Custom `SkillAdapter` class-path signed + allowlisted; fail-closed unsigned at ent/federal (REQ-003) |
| **LLM09 misinformation** — judge over-trusts | Judge only *ranks*; deterministic golden tasks *decide* (REQ-022) |
| **Excessive agency** (federal) | Operator approves every mutation + retire (§7, SPEC-043 ladder) |

### Research Insights — solutions-archive hardening (two directly-applicable prior findings)

Both are `SPEC-017` security-review learnings that map straight onto this spec's change-bound and tier plumbing.

1. **"AST Validator Is Not Enough — Defense-in-Depth for Dynamic Code Sandboxes"** (`.claude/solutions/security-issues/2026-04-18-ast-validator-is-not-enough-defense-in-depth.md`). Static validation of untrusted code is inherently incomplete (RestrictedPython shipped 3 CVEs in 2 years; Python's reflection surface is vast). **Consequence for REQ-030:** `max_ast_distance` is a **change-bound *regularizer*, never a security boundary.** The security boundary for a code patch is the **sandboxed golden-task eval** (`hub.dry_run`, DC-5) + agent-DID re-sign + hub re-verify + hub scanner — a small AST edit distance must NEVER be read as "this patch is safe." The SkillOpt-faithful `max_edits` bound and `max_ast_distance` both exist to keep *optimization* convergent and reviewable, not to contain a malicious patch. The §8 table's ASI05 row already asserts sandbox-decides; this pins the *why* and forbids an AST-only shortcut.

2. **"Tier / Deployment-Context Values Must Flow Through Construction, Not Per-Call"** (`.claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md`). A tier hardcoded in a per-call path made federal audit events record `tier=personal` while enforcement was correct — *the audit lied.* **Consequence for REQ-031/change-bound resolution:** `tier` must be bound at `ArcSkillImprover` **construction** (from `skilladapt.select`, itself from config), and every `ChangeBound.check(...)`, `MutationEvent`, and `LifecycleEvent` must carry that constructed tier — never a per-call default like `tier="personal"`. The federal-floor-non-relaxable guarantee and the operator-signed audit trail are only trustworthy if tier flows from construction. Add an assertion/test that a federal-constructed improver stamps `tier=federal` on emitted audit events (analogue of the 95-test fix in the cited solution).

## 9. Alternatives considered

- **Keep improver in arcagent, add code-repair in place.** Rejected: violates concern boundary (skill self-modification is an optional capability that belongs in a dedicated package, not the agent core), keeps arcagent bloated, blocks the SPEC-047 extension-point shape.
- **Standalone sibling package (`arcevolve`).** Rejected (Josh, 2026-07-08, FINAL — reverses an earlier misread of his steering): "arcskill IS the optional package; arcagent already manages skills; arcskill supercharges them." A second optional package fragments the supercharger surface for zero optionality gain — arcskill is already absent unless installed, and improvement inside it is config-gated, so installing arcskill does not force self-modification on.
- **arcskill depends on arcllm directly.** Rejected: couples pure logic to a provider and breaks arch-test cleanliness; injected `Mutator`/`Judge` seams (arcmemory `Distiller` precedent) keep arcskill provider-free.
- **LLM-judge as the acceptance gate (status quo).** Rejected: a judge cannot validate code correctness; deterministic golden tasks are the gate, judge ranks.
- **Retire = delete the skill.** Rejected: irreversible; retire = disable + retain lineage (revivable).

## 10. Open questions

See README §Open Questions (OQ-1..OQ-9) — each resolved with a recommended default; items pinned by /deepen are marked **[DEEPEN]** (SkillOpt change-bound constants; Hermes self-adaptation approach).
