# SDD: Skill Improver Module

**Spec ID**: SPEC-012
**Date**: 2026-02-25

## 1. Component Overview

| Component | File | Estimated LOC | Type |
|-----------|------|---------------|------|
| Module facade | `modules/skill_improver/skill_improver_module.py` | ~150 | New |
| Config | `modules/skill_improver/config.py` | ~60 | New |
| Data models | `modules/skill_improver/models.py` | ~80 | New |
| Trace collector | `modules/skill_improver/trace_collector.py` | ~120 | New |
| Optimization engine | `modules/skill_improver/engine.py` | ~250 | New |
| Pareto frontier | `modules/skill_improver/pareto.py` | ~150 | New |
| Reflector | `modules/skill_improver/reflector.py` | ~120 | New |
| Evaluator | `modules/skill_improver/evaluator.py` | ~200 | New |
| Candidate store | `modules/skill_improver/candidate_store.py` | ~150 | New |
| Guardrails | `modules/skill_improver/guardrails.py` | ~120 | New |
| MODULE.yaml | `modules/skill_improver/MODULE.yaml` | N/A | New |
| __init__.py | `modules/skill_improver/__init__.py` | ~10 | New |

**Total estimated LOC**: ~1,410 (within 1,400 budget, may need minor budget revision after formatting)

## 2. Data Models (`models.py`)

### 2.1 Trace Models

```python
@dataclass(frozen=True)
class ToolCallRecord:
    tool_name: str
    args_hash: str           # SHA-256 of sanitized args
    result_status: str       # "ok" | "error" | "vetoed"
    duration_ms: float
    error_type: str | None   # e.g. "PermissionError", None on success

@dataclass
class SkillTrace:
    trace_id: str            # Matches OTel trace_id
    session_id: str
    skill_name: str
    skill_version: int       # From candidate store, 0 = seed
    turn_number: int
    started_at: datetime
    ended_at: datetime | None
    tool_calls: list[ToolCallRecord]
    expected_tools: list[str]    # Parsed from skill markdown
    coverage_pct: float          # Actual vs expected
    task_summary: str            # First 200 chars of task context
    task_outcome: str | None     # "success" | "failure" | "partial"
    outcome_source: str | None   # "heuristic" | "evaluator"
```

### 2.2 Candidate Models

```python
@dataclass
class Candidate:
    id: str                          # UUID
    text: str                        # Full skill body text
    scores: dict[str, list[float]]   # dimension -> per-trace scores
    aggregate_scores: dict[str, float]  # dimension -> mean
    token_count: int
    parent_id: str | None
    generation: int                  # Distance from seed
    created_at: datetime
    fingerprint: str                 # SHA-256 of text for quick comparison

@dataclass
class OptimizeResult:
    skill_name: str
    best_candidate: Candidate
    frontier: list[Candidate]
    iterations_run: int
    stop_reason: str            # "stagnation" | "max_iterations" | "guardrail"
    seed_scores: dict[str, float]
    improvement: dict[str, float]  # Per-dimension delta from seed
```

### 2.3 Audit Models

```python
@dataclass(frozen=True)
class MutationEvent:
    timestamp: datetime
    skill_name: str
    previous_hash: str        # SHA-256 of old text
    new_hash: str             # SHA-256 of new text
    candidate_id: str
    generation: int
    scores: dict[str, float]
    improvement: dict[str, float]
    stop_reason: str
    trace_ids: list[str]      # Which traces were used
```

## 3. Trace Collector (`trace_collector.py`)

### 3.1 Detection Mechanism

```python
class TraceCollector:
    def __init__(self, skill_registry: SkillRegistry, workspace: Path, config: SkillImproverConfig):
        self._skill_paths: dict[Path, str] = {}  # resolved path -> skill name
        self._active_span: SkillTrace | None = None
        self._usage_counts: dict[str, int] = {}  # skill_name -> use count

    def index_skills(self, skill_registry: SkillRegistry) -> None:
        """Build path -> name lookup from SkillRegistry."""
        for skill in skill_registry.list_skills():
            self._skill_paths[skill.file_path.resolve()] = skill.name

    async def on_post_tool(self, ctx: EventContext) -> None:
        """Priority 200. Detect skill reads, capture tool calls."""
        tool = ctx.data.get("tool", "")
        if tool == "read":
            path = Path(ctx.data.get("args", {}).get("file_path", "")).resolve()
            if path in self._skill_paths:
                self._close_span()
                self._open_span(self._skill_paths[path], ctx)
        elif self._active_span is not None:
            self._record_tool_call(ctx)

    async def on_post_plan(self, ctx: EventContext) -> None:
        """Priority 200. Close span at turn end."""
        self._close_span()
```

### 3.2 Storage

JSONL files at `{workspace}/skill_traces/{skill_name}/traces-{YYYY-MM}.jsonl`. One JSON line per `SkillTrace`.

Index file at `{workspace}/skill_traces/{skill_name}/index.json`:
```json
{
  "total_traces": 47,
  "success_count": 38,
  "failure_count": 9,
  "last_optimized_at": "2026-02-25T14:30:00Z",
  "current_generation": 2,
  "seed_token_count": 340
}
```

## 4. Optimization Engine (`engine.py`)

### 4.1 Core Loop

```python
class SkillOptimizer:
    async def optimize(self, skill_name: str) -> OptimizeResult | None:
        # 1. Load current skill text + traces
        traces = self._store.load_eligible_traces(skill_name)
        current_text = self._load_skill_text(skill_name)

        # 2. Check guardrails
        if not self._guardrails.check_eligible(skill_name, traces):
            return None

        # 3. Split traces: 70% train, 30% holdout
        train, holdout = self._split_traces(traces, ratio=0.7)

        # 4. Initialize frontier with current text (generation 0)
        seed = self._create_seed_candidate(current_text, holdout)
        frontier = ParetoFrontier()
        frontier.add(seed)

        # 5. Optimization loop
        stagnation_count = 0
        for i in range(self._config.max_iterations):
            # Select parent from frontier
            parent = frontier.select()

            # Evaluate parent on train minibatch
            minibatch = self._sample_minibatch(train)
            parent_eval = await self._evaluator.evaluate(parent.text, minibatch)

            # Reflect on failures -> constrained mutation
            failures = [t for t, s in zip(minibatch, parent_eval.per_trace)
                       if s.aggregate < 3.0]
            if not failures:
                continue

            mutation_text = await self._reflector.reflect(
                parent.text, failures, self._get_intent_header(skill_name)
            )

            # Validate mutation against guardrails
            mutation = self._create_candidate(mutation_text, parent)
            if not self._guardrails.validate_candidate(mutation, seed):
                continue

            # Evaluate mutation on holdout
            mutation_eval = await self._evaluator.evaluate(mutation.text, holdout)
            mutation.scores = mutation_eval.per_trace_scores
            mutation.aggregate_scores = mutation_eval.aggregate_scores
            mutation.token_count = self._count_tokens(mutation.text)

            # Add to frontier if not dominated AND min-delta exceeded
            if frontier.add_if_improves(mutation, min_delta=self._config.min_delta):
                stagnation_count = 0
            else:
                stagnation_count += 1

            if stagnation_count >= self._config.stagnation_limit:
                break

        # 6. Return best
        best = frontier.overall_best()
        return OptimizeResult(...)
```

### 4.2 Post-Optimization Application

After engine returns:
1. `candidate_store.save(best_candidate)` — persist with full lineage
2. `atomic_write_text(skill_path, best_candidate.text)` — update skill file
3. `skill_registry.rescan_agent_created()` — refresh registry
4. `audit_log.append(MutationEvent(...))` — append-only audit

## 5. Pareto Frontier (`pareto.py`)

### 5.1 Per-Example Dominance

```python
class ParetoFrontier:
    def dominates(self, a: Candidate, b: Candidate) -> bool:
        """A dominates B iff A >= B on every trace score AND > on at least one.
        Token count is an additional dimension (lower is better)."""
        a_scores = self._score_vector(a)  # per-trace scores + inverted token ratio
        b_scores = self._score_vector(b)
        at_least_one_better = False
        for sa, sb in zip(a_scores, b_scores):
            if sa < sb:
                return False
            if sa > sb:
                at_least_one_better = True
        return at_least_one_better

    def add(self, candidate: Candidate) -> bool:
        """Add if not dominated. Evict any candidates it dominates."""
        for existing in self._candidates:
            if self.dominates(existing, candidate):
                return False  # Dominated by existing
        self._candidates = [c for c in self._candidates
                           if not self.dominates(candidate, c)]
        self._candidates.append(candidate)
        return True

    def add_if_improves(self, candidate: Candidate, min_delta: float) -> bool:
        """Add only if candidate exceeds its parent by min_delta on at least one dimension."""
        if candidate.parent_id is None:
            return self.add(candidate)
        parent = self._find(candidate.parent_id)
        if parent is None:
            return self.add(candidate)
        # Must exceed parent on at least one dimension by min_delta
        for dim in candidate.aggregate_scores:
            if candidate.aggregate_scores[dim] - parent.aggregate_scores.get(dim, 0) >= min_delta:
                return self.add(candidate)
        return False

    def select(self) -> Candidate:
        """Random selection from frontier, weighted by coverage."""
        # Weight by number of traces where candidate is best
        ...

    def overall_best(self) -> Candidate:
        """Highest average across all dimensions."""
        return max(self._candidates,
                  key=lambda c: sum(c.aggregate_scores.values()) / len(c.aggregate_scores))
```

## 6. Evaluator (`evaluator.py`)

### 6.1 Per-Dimension RaR Evaluation

```python
class SkillEvaluator:
    DIMENSIONS = {
        "accuracy": {
            "checklist": [
                "All steps lead to correct outcomes",
                "No incorrect or misleading instructions",
                "Prerequisites are correctly stated",
                "Success criteria are verifiable",
                "Edge cases are handled correctly",
            ],
            "anti_inflation": "A score of 5 requires ZERO factual errors. Most procedures score 2-4.",
        },
        "efficiency": {
            "checklist": [
                "No redundant or unnecessary steps",
                "Steps are in optimal order",
                "No unnecessary tool calls implied",
                "Procedure achieves goal in minimal steps",
                "No repeated information",
            ],
            "anti_inflation": "A score of 5 requires every step to be essential. Most procedures score 2-4.",
        },
        "error_handling": {
            "checklist": [
                "Common failure modes are anticipated",
                "Recovery steps are provided for errors",
                "Fallback paths are defined",
                "Error messages guide next actions",
                "Partial failure scenarios are addressed",
            ],
            "anti_inflation": "A score of 5 requires comprehensive error coverage. Most procedures score 2-3.",
        },
        "clarity": {
            "checklist": [
                "Each step has a single unambiguous action",
                "Technical terms are defined or standard",
                "Conditional branches specify both paths",
                "Success criteria are explicitly stated",
                "A practitioner can execute without interpretation",
            ],
            "anti_inflation": "A score of 5 requires ZERO ambiguity. Most procedures score 2-4.",
        },
    }

    async def evaluate_dimension(
        self, skill_text: str, trace: SkillTrace, dimension: str
    ) -> DimensionScore:
        """One LLM call per dimension per trace. Returns checklist results + score."""
        checklist = self.DIMENSIONS[dimension]["checklist"]
        prompt = self._build_judge_prompt(skill_text, trace, dimension, checklist)
        response = await self._llm.invoke(prompt)
        return self._parse_score(response)

    async def evaluate(
        self, skill_text: str, traces: list[SkillTrace]
    ) -> EvalResult:
        """Evaluate skill across all dimensions and traces."""
        per_trace_scores = []
        for trace in traces:
            dim_scores = {}
            for dim in self._config.eval_dimensions:
                dim_scores[dim] = await self.evaluate_dimension(skill_text, trace, dim)
            per_trace_scores.append(dim_scores)
        return EvalResult(per_trace_scores=per_trace_scores, ...)
```

### 6.2 Judge Prompt Structure

```
System: You are evaluating a skill procedure document on {DIMENSION}.

CALIBRATION EXAMPLES:
Score 1 (Poor): [anchor example for this dimension]
Score 3 (Moderate): [anchor example]
Score 5 (Excellent): [anchor example]

{ANTI_INFLATION_INSTRUCTION}

CHECKLIST (answer YES or NO for each):
{CHECKLIST_ITEMS}

EXECUTION TRACE:
Task: {trace.task_summary}
Tool calls: {formatted_tool_calls}
Errors: {formatted_errors}
Outcome: {trace.task_outcome}
Coverage: {trace.coverage_pct}%

PROCEDURE TO EVALUATE:
{skill_text}

First, evaluate each checklist item with YES/NO and brief reasoning.
Then provide your score (1-5) = count of YES answers.

Respond in JSON:
{"checklist": [{"item": str, "answer": bool, "reason": str}], "score": int, "rationale": str}
```

## 7. Reflector (`reflector.py`)

### 7.1 Constrained Mutation

```python
class SkillReflector:
    async def reflect(
        self,
        current_text: str,
        failures: list[tuple[SkillTrace, dict[str, DimensionScore]]],
        intent_header: str,
    ) -> str:
        """Propose targeted mutation based on failure analysis."""
        # Identify which sections/dimensions are weakest
        weak_dimensions = self._identify_weak_dimensions(failures)
        failure_patterns = self._extract_patterns(failures)

        prompt = self._build_reflection_prompt(
            current_text, weak_dimensions, failure_patterns, intent_header
        )
        response = await self._llm.invoke(prompt)
        return self._extract_candidate(response)
```

### 7.2 Reflection Prompt Structure

```
You are improving a skill procedure document.

RULES:
- DO NOT modify the SKILL INTENT [IMMUTABLE] section
- Focus your revision ONLY on: {weak_dimensions}
- DO NOT add unnecessary caveats or hedging language
- The revised skill must be under {token_budget} tokens
- Produce specific, actionable steps — not descriptions

CURRENT SKILL:
{current_text}

FAILURE PATTERNS (across {N} execution traces):
{failure_patterns}

WEAKEST DIMENSIONS:
{weak_dimension_analysis}

Identify the root cause pattern across these failures.
Then produce an improved version of the skill inside ```markdown``` fences.
```

## 8. Guardrails (`guardrails.py`)

```python
class Guardrails:
    def check_eligible(self, skill_name: str, traces: list[SkillTrace]) -> bool:
        """Pre-optimization checks."""
        if len(traces) < self._config.min_traces:
            return False
        if self._in_cooloff(skill_name):
            return False
        if self._is_exempt(skill_name):
            return False
        if self._get_generation(skill_name) >= self._config.max_generations:
            return False
        return True

    def validate_candidate(self, candidate: Candidate, seed: Candidate) -> bool:
        """Post-mutation checks before frontier admission."""
        # Intent header preserved
        if not self._intent_preserved(candidate.text, seed.text):
            return False
        # Token budget
        if candidate.token_count > seed.token_count * self._config.max_token_ratio:
            return False
        # Anchor distance (requires embedding)
        if self._anchor_distance(candidate.text, seed.text) > self._config.anchor_distance_threshold:
            return False
        # Oscillation detection
        if self._is_oscillation(candidate, self._recent_versions(seed.id, n=5)):
            return False
        return True
```

## 9. Candidate Store (`candidate_store.py`)

### 9.1 Storage Layout

```
{workspace}/skill_traces/{skill_name}/
├── traces-2026-02.jsonl     # Execution traces
├── index.json               # Aggregate stats
├── candidates/
│   ├── seed.md              # Original skill text (never modified)
│   ├── {candidate_id}.md    # Each candidate version
│   └── manifest.json        # Frontier state, lineage, active pointer
└── audit.jsonl              # Append-only mutation log
```

### 9.2 Manifest

```json
{
  "skill_name": "plan-business-travel",
  "seed_hash": "sha256:...",
  "seed_token_count": 340,
  "active_candidate_id": "abc123",
  "generation": 2,
  "frontier": ["abc123", "def456"],
  "candidates": {
    "seed": {"generation": 0, "parent_id": null, "scores": {...}},
    "abc123": {"generation": 1, "parent_id": "seed", "scores": {...}},
    "def456": {"generation": 2, "parent_id": "abc123", "scores": {...}}
  },
  "last_optimized_at": "2026-02-25T14:30:00Z",
  "cooloff_until": null,
  "total_optimization_runs": 3
}
```

## 10. Module Facade (`skill_improver_module.py`)

### 10.1 Event Subscriptions

| Event | Priority | Handler | Purpose |
|-------|----------|---------|---------|
| `agent:post_tool` | 200 | `_on_post_tool` | Detect skill reads, capture tool calls |
| `agent:post_plan` | 200 | `_on_post_plan` | Close trace spans at turn end |
| `agent:post_respond` | 150 | `_on_post_respond` | Check usage threshold, trigger optimization |
| `agent:ready` | 100 | `_on_ready` | Index skill paths from registry |

### 10.2 Tools

| Tool | Transport | Purpose |
|------|-----------|---------|
| `skill_versions` | NATIVE | List optimization history for a skill |
| `skill_rollback` | NATIVE | Revert a skill to a previous version (triggers cooloff) |

### 10.3 Trigger Flow

```python
async def _on_post_respond(self, ctx: EventContext) -> None:
    # Check each skill's usage count
    for skill_name, count in self._collector.usage_counts.items():
        if count >= self._config.optimize_after_uses:
            self._collector.reset_count(skill_name)
            spawn_background(
                self._optimize_skill(skill_name),
                background_tasks=self._background_tasks,
                semaphore=self._semaphore,
                eval_config=self._eval_config,
                telemetry=self._telemetry,
                audit_event_name="skill_improver.optimization_error",
                logger=_logger,
            )
```

## 11. Dependency on arcllm

All LLM calls (evaluation + reflection) go through arcllm via the eval model configured in `eval_config`. No direct LLM provider calls. No new dependencies beyond what arcagent already provides.

## 12. Embedding for Anchor Distance

Anchor distance requires text embeddings. Two options:

1. **Lightweight: character n-gram similarity** — No LLM needed. Use `difflib.SequenceMatcher` ratio as a proxy for semantic distance. Less accurate but zero-cost.
2. **Accurate: LLM embedding** — Use arcllm's embedding endpoint if available.

Decision: Start with option 1 (SequenceMatcher). Upgrade to embeddings if drift detection proves insufficient. This avoids adding an embedding dependency for v1.
