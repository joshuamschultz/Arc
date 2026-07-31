# SPEC-012: Skill Improver Module

| Field | Value |
|-------|-------|
| **ID** | SPEC-012 |
| **Feature** | skill-improver |
| **Type** | Module / AI-Optimization |
| **Status** | COMPLETE |
| **Created** | 2026-02-25 |
| **Package** | `packages/arcagent/` |
| **LOC Budget** | 1,400 |

## Prior Work

| Stage | Output | Date |
|-------|--------|------|
| Research | GEPA repo analysis (gepa-ai/gepa) | 2026-02-25 |
| Research | ArcAgent module system analysis | 2026-02-25 |
| Research | ArcAgent skill system analysis | 2026-02-25 |
| Deepen | 5 research agents (evolutionary optimization, LLM-as-judge, module patterns, trace collection, mutation risks) | 2026-02-25 |

## Design Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D1 | External dependency | No GEPA dependency — build own engine | Avoid external dep, tailor to arcagent, use arcllm for LLM calls |
| D2 | What gets optimized | Skill body text only (not identity, not name/description) | Identity is sacred; name/description is the skill's interface |
| D3 | Operation mode | Fully autonomous once enabled in config | No per-change human gate; human gate = enabling the module |
| D4 | Trigger mechanism | Turn-count based (after N skill uses) via spawn_background | Same pattern as bio_memory consolidation and policy ACE |
| D5 | Pareto strategy | Per-example Pareto (not per-aggregate) | Research: single most impactful design decision for quality + diversity |
| D6 | Evaluation scale | 1-5, one dimension per LLM call, RaR checklist | LLMs unreliable at 10 gradations; multi-dim single-call unreliable |
| D7 | Mutation strategy | Constrained, targeted mutations (not free-form) | EvoPrompt: 69.87% → 75.55% accuracy with targeted mutations |
| D8 | Trace detection | Deterministic path match against SkillRegistry | No inference needed; match read tool paths to registered skill file_paths |
| D9 | Trace boundaries | Turn-based (pre_plan → post_plan) | Cleanest, non-overlapping spans |
| D10 | Min traces | 30 before any optimization | Below 30, noise exceeds signal |
| D11 | Safety: anchor distance | Cosine distance from seed < 0.15 | Prevents semantic drift from original intent |
| D12 | Safety: token budget | seed_tokens * 1.5 hard ceiling | Prevents length explosion |
| D13 | Safety: immutable intent | `## SKILL INTENT [IMMUTABLE]` header block | Optimizer forbidden from modifying |
| D14 | Safety: generation limit | Max 10 generations from seed before suspension | Prevents unbounded drift |
| D15 | Safety: oscillation detection | Fingerprint versions, detect cycling | Halt if new candidate within 0.05 cosine of recent versions |
| D16 | Safety: temporal buffer | Traces must age N turns before entering pool | Breaks feedback loops |
| D17 | Safety: exempt skills | Skills tagged security-critical/compliance/auth | Never autonomously optimized |
| D18 | Versioning | Internal candidate store (optimization-aware, not general) | Tracks Pareto dominance, lineage, merge history |
| D19 | Storage | JSONL per skill, monthly rotation | ~2-4 KB per trace, trivially small |
| D20 | Crossover | DE-style merge (base + diff) | More reliable than GA crossover for text |

## Research Insights Summary

- Per-example Pareto selection is the core design decision (GEPA, CAPO)
- LLM judges achieve ~0.85 agreement with humans when calibrated with anchor examples
- Min-delta acceptance gate prevents frontier from filling with weak improvements
- Constrained mutations dramatically outperform free-form rewrites
- TextGrad worst case: 96% validation → 69% test — holdout set mandatory
- Autonomous mutation requires: anchor distance, token budget, generation counter, oscillation detection
- `spawn_background` pattern with semaphore + backpressure is proven in bio_memory and policy modules
- Append-only audit log required for NIST AU-3 compliance
- Cooling-off after rollback prevents re-creating same bad version

## Risks

1. **LLM judge reliability in specialized domain**: Procedure evaluation is expert domain; expect ~60-68% agreement without calibration. Mitigated by RaR checklist pattern + anchor examples.
2. **Trace attribution accuracy**: Research shows ~53% accuracy for failure attribution. Mitigated by using coverage metrics (deterministic) alongside LLM evaluation.
3. **Feedback loops**: Agent uses optimized skill → generates traces → optimizes further. Mitigated by temporal buffer + trace diversity requirements + independent judge model.
4. **Token cost**: 4 LLM calls per evaluation (1/dimension) × 10 iterations = 40+ eval calls per optimization run. Mitigated by minibatch evaluation and on/off config.
5. **No prior art for markdown procedure optimization**: Most research covers short prompts, not structured procedural docs. Proceeding with awareness.

## Related Specs

- BIO-001, BIO-002: Bio-memory module (same module architecture pattern)
- SPEC-003: Module decoupling (module bus architecture)
