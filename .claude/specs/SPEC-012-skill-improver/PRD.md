# PRD: Skill Improver Module

**Spec ID**: SPEC-012
**Date**: 2026-02-25
**Source**: GEPA research + 5 research agents + arcagent module analysis

## 1. Problem Statement

ArcAgent skills (markdown procedure files) are static once written. An agent may use a skill hundreds of times, encountering failures, inefficiencies, and edge cases — but the skill text never improves. Insights are lost. The same mistakes recur. Skills that could be refined through operational experience remain frozen at their initial quality level.

## 2. Goals

1. **Autonomous skill improvement** — Continuously improve skill body text through evolutionary optimization using collected execution traces
2. **Safety-constrained evolution** — Prevent catastrophic regression, semantic drift, length explosion, and feedback loops through research-validated guardrails
3. **Zero core changes** — Pure module implementation following existing module architecture (MODULE.yaml, Module protocol, bus events, tool registration)
4. **Federal compliance** — Append-only audit trail on all mutations for NIST AU-3

## 3. Non-Goals

- Modifying `identity.md` or any non-skill text artifact
- Modifying skill names or descriptions (the "interface")
- General-purpose text versioning infrastructure
- Real-time optimization during agent execution (batch only, background)
- Requiring human approval per change (human gate = enabling the module)

## 4. Requirements

### 4.1 Trace Collection (passive)

| ID | Requirement | Priority |
|----|-------------|----------|
| R1 | Detect skill usage by matching `read` tool paths against `SkillRegistry.file_path` | P0 |
| R2 | Open skill execution span on skill read, close at turn end (`agent:post_plan`) | P0 |
| R3 | Capture tool call records: tool_name, args_hash (SHA-256), result_status, duration_ms, error_type | P0 |
| R4 | Parse expected tools from skill markdown at discovery time | P1 |
| R5 | Compute deterministic coverage metric (actual vs expected tool sequence) | P1 |
| R6 | Store traces as JSONL per skill with monthly rotation | P0 |
| R7 | Never store raw tool args, user input, or LLM reasoning (hash/truncate only) | P0 |
| R8 | Trace size target: 2-4 KB per trace | P1 |

### 4.2 Optimization Engine

| ID | Requirement | Priority |
|----|-------------|----------|
| R9 | Per-example Pareto frontier (A dominates B iff A >= B on every trace, > on at least one) | P0 |
| R10 | Min-delta acceptance gate (child must exceed parent by threshold before entering frontier) | P0 |
| R11 | Constrained, targeted mutations (reflector told which section to focus on, which to preserve) | P0 |
| R12 | Token count as explicit Pareto dimension (evolutionary pressure against length) | P0 |
| R13 | Frontier stagnation stop (no new candidate in 5 consecutive generations) | P0 |
| R14 | Minibatch for mutation feedback (subset of traces), full eval periodic (every 5 generations) | P1 |
| R15 | DE-style merge/crossover (base + diff between two frontier candidates) | P1 |
| R16 | Exponential moving average of performance signals (momentum, prevents oscillation) | P1 |

### 4.3 Evaluation (LLM-as-Judge)

| ID | Requirement | Priority |
|----|-------------|----------|
| R17 | 1-5 scale, one dimension per LLM call | P0 |
| R18 | Four dimensions: accuracy, efficiency, error_handling, clarity | P0 |
| R19 | RaR (Rubrics as Rewards) checklist pattern per dimension | P0 |
| R20 | Anchor examples in every judge prompt (calibration) | P0 |
| R21 | Anti-inflation instruction ("most procedures should score 2-4") | P0 |
| R22 | Chain-of-thought before score (structured JSON output) | P0 |
| R23 | Feed full execution traces (ASI) to judge, not just outcomes | P0 |
| R24 | Use eval model (configured via eval_config), not agent model | P0 |

### 4.4 Safety Guardrails

| ID | Requirement | Priority |
|----|-------------|----------|
| R25 | Minimum 30 traces before any optimization run | P0 |
| R26 | Trace diversity check (similarity filter, cosine > 0.85 = duplicate) | P1 |
| R27 | Immutable intent header (`## SKILL INTENT [IMMUTABLE]`) — reflector forbidden, validator rejects changes | P0 |
| R28 | Semantic anchor distance (cosine from seed < 0.15, configurable) | P0 |
| R29 | Hard token budget (seed_tokens * 1.5 ceiling) | P0 |
| R30 | Generation counter (max 10 from seed before suspension) | P0 |
| R31 | Oscillation detection (fingerprint within 0.05 cosine of recent 5 versions → halt) | P1 |
| R32 | Temporal trace buffer (traces must age N turns before entering pool) | P0 |
| R33 | Cooling-off after rollback (configurable, default 200 turns) | P0 |
| R34 | Security-critical skill exemption (by frontmatter tag) | P0 |
| R35 | Append-only audit log for every mutation event | P0 |

### 4.5 Module Integration

| ID | Requirement | Priority |
|----|-------------|----------|
| R36 | MODULE.yaml manifest following convention | P0 |
| R37 | Module protocol: name property, startup(ctx), shutdown() | P0 |
| R38 | Constructor injection: config, eval_config, telemetry, workspace, llm_config | P0 |
| R39 | Disabled by default in config (`enabled = false`) | P0 |
| R40 | Background execution via `spawn_background` (fire-and-forget) | P0 |
| R41 | Trigger via turn-count after N skill uses (configurable, default 50) | P0 |
| R42 | Two tools: `skill_versions` (read-only), `skill_rollback` | P1 |
| R43 | Atomic file writes via `atomic_write_text` | P0 |
| R44 | Telemetry audit events for all operations | P0 |

## 5. Success Metrics

| Metric | Target |
|--------|--------|
| Module loads and collects traces without errors | Pass |
| Optimization produces candidates that score higher than seed on holdout traces | Measurable improvement |
| No skill ever exceeds token budget | 100% |
| No skill drifts beyond anchor distance threshold | 100% |
| Audit log captures every mutation with full lineage | 100% |
| Zero changes to core arcagent code | 0 files in core/ modified |

## 6. Config Surface

```toml
[modules.skill_improver]
enabled = false

[modules.skill_improver.config]
min_traces = 30
trace_buffer_turns = 50
trace_similarity_threshold = 0.85
optimize_after_uses = 50
max_iterations = 10
stagnation_limit = 5
eval_dimensions = ["accuracy", "efficiency", "error_handling", "clarity"]
eval_scale = 5
max_token_ratio = 1.5
max_generations = 10
anchor_distance_threshold = 0.15
cooloff_turns = 200
exempt_tags = ["security-critical", "compliance", "auth"]
```
