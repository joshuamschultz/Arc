# Solution Design Document: SPEC-060 — Memory Ingestion & LongMemEval Evaluation

## Context References

- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Project structure:** [.claude/steering/structure.md](../../steering/structure.md)
- **PRD:** [PRD.md](./PRD.md)

## Overview

A standalone `evaluations/` tree at the repo root that drives the unmodified Arc stack as a black box. It splits into two halves with a hard seam between them: `evaluations/ingest/` is source-agnostic (adapter protocol, chunker, fidelity gate, agent factory, driver, consolidation waiter) and `evaluations/longmemeval/` is the first and only consumer (dataset, judge, scoring, phases, resume, budget, preflight). Email and Slack attach to `ingest/` later without touching a line of it.

Zero framework changes. Every requirement is met by configuration, by calling functions already exported in a package's `__all__`, or by harness-side code. Nothing under `packages/` imports `evaluations/`, and `evaluations/` is not a uv workspace member, so the dependency DAG in [structure.md](../../steering/structure.md) is untouched.

The measurement posture is deliberate and recorded rather than optimized: personal tier, `workpad` and `policy` modules left ON, full production agent surface. The reported number therefore measures arcmemory *plus* two additional system-prompt summarizers, and the manifest says so.

## Architecture

**Execution model — strictly sequential.** One question at a time, one live `ArcAgent` at a time. This is the single largest simplifying decision in the design: it removes the shared flocked WORM audit chain, the shared `~/.arc/store` SQLite, provider rate-limit cascades, and `contextvars` sibling-task hazards from scope entirely. No semaphore, no `asyncio.gather`, no backpressure logic exists in this harness.

**The loop, per question:**

```
PhaseRunner (sequential)
  └─ for each question:
       Preflight (once per phase) ──► abort phase on any failure
       WorkspaceLifecycle.create()          # throwaway run dir
       EvalAgentFactory.build()             # render → scaffold → load_config → ArcAgent → startup()
       for each session in Adapter.read():
         TurnChunker.split()                # turn boundaries, ~1700 chars, \n between turns
         SanitizeFidelityGate.check()       # local sanitize+privacy_filter, VOID on gold loss
         IngestDriver.feed()                # run_collected() per chunk, per-chunk session key
         ConsolidationWaiter.wait()         # await consolidate_poll_once() + quiescence asserts
       WorkspaceLifecycle.mark_ingest_complete()
       QueryRunner.ask()                    # [Current date: …] + question
       JudgeAgent.score()                   # separate agent, gpt-4o-2024-08-06, temp 0
       ScoringEngine.retrieval()            # recall_any@k / recall_all@k
       ArtifactScrubber.clean() ──► ResultLedger.append()   # status: complete
       WorkspaceLifecycle.teardown()        # finally: WAL checkpoint, rm -rf
```

**Two dates, two sides.** This is the design's temporal spine. The *write* side carries each session's date into memory as content (`[Session date: YYYY-MM-DD]` prefix on **every** chunk — one chunk is one ingest turn, so each successive turn re-inputs the date and a session split across five chunks yields five dated chunks) because `FastCapture` hard-codes `Event.ts` to `now()` and no public capture signature accepts a source timestamp. Repeating rather than stamping once is what keeps the date reachable no matter which chunk a retrieval lands on. The *read* side carries the question's date into the query turn (`[Current date: YYYY-MM-DD]`) because Arc's system prompt contains no date at all — verified: `assemble_system_prompt` builds from `identity.md`, `context.md` and module-injected sections only (`core/session_internal/context.py:240-252`), and no date appears in the prompt path, the arcrun strategies, or the memory module. Both are harness-side text prefixes; neither touches the framework.

**Directory layout** (follows [structure.md](../../steering/structure.md) naming; `evaluations/` is a peer of `packages/`, not a member):

```
evaluations/
  README.md
  ingest/                 # source-agnostic — the reusable pathway
    types.py  adapter.py  chunker.py  fidelity.py
    agent_factory.py  driver.py  consolidation.py  lifecycle.py
  longmemeval/            # the first consumer
    adapter.py  dataset.py  query.py  judge.py  scoring.py
    reference_prompts.py  agreement.py
    runner.py  ledger.py  preflight.py  budget.py  manifest.py
    hygiene.py  scrub.py  cli.py
    config/arcagent.toml.example  arcllm.toml.example  arcrun.toml.example
  data/                   # gitignored — dataset JSON
  runs/                   # gitignored — throwaway workspaces (NO harness code here, ever)
  results/                # gitignored — JSONL + run_manifest.json
```

**Gitignore gotcha driving that layout:** git cannot re-include a file beneath an ignored directory, so a broad `evaluations/**/runs/` permanently swallows anything under it and a `!` negation will not rescue it. Harness code therefore never lives in `runs/`, and TOML files are named individually (`arcagent.toml`, `arcllm.toml`, `arcrun.toml`) rather than by a blanket `*.toml` glob. Measured during implementation: a blanket `*.toml` hides 41 tracked files — every `pyproject.toml` plus all five `blueprints/*/blueprint.toml`. It would *not* touch the `config/*.toml.example` templates, since those end in `.example`; the prohibition is right but the original rationale was not.

**Trust boundary.** Benchmark text is untrusted third-party content (LLM01, LLM04, ASI06). It crosses the boundary the framework already has — `sanitize()` → `privacy_filter()` → windowed dedup inside `FastCapture` — and the harness never bypasses it, because bypassing it would both violate the boundary and invalidate the measurement (D-493). What the harness adds is a *mirror* of that filter run locally beforehand, so filter damage to gold evidence is detected and the question voided rather than silently scored.

## Components

### COMP-001: SourceAdapter protocol + session types
**Responsibility:** Define the one seam every corpus attaches to: a `read()` yielding `Session` objects of ordered `Turn`s, each session carrying a source date and a conversation id. Pydantic models at the boundary per CON-2. This module imports nothing from `longmemeval/`.
**Dependencies:** pydantic
**Inputs:** none (protocol definition)
**Outputs:** `class SourceAdapter(Protocol): def read(self) -> Iterator[Session]`; `Session {conversation_id: str, source_date: date, turns: list[Turn]}`; `Turn {role: str, text: str, turn_id: str}`

### COMP-002: LongMemEvalAdapter
**Responsibility:** The only adapter in this release. Maps one LongMemEval question's haystack into `Session` objects in the dataset's own order, and exposes the question's own metadata (question text, type, gold answer, `answer_session_ids`, `has_answer` flags, and the question date). Resolves the question date from the dataset field when present, otherwise derives it as the latest `haystack_dates` entry, and reports which source was used.
**Dependencies:** COMP-001, COMP-003
**Inputs:** `question_id: str`, loaded dataset
**Outputs:** `read() -> Iterator[Session]` in pinned dataset order; `question_meta() -> QuestionMeta {question, question_type, answer, answer_session_ids, has_answer_turns, question_date, question_date_source: Literal['dataset','derived']}`

### COMP-003: DatasetLoader
**Responsibility:** Load `longmemeval_oracle.json` / `longmemeval_s_cleaned.json` from the gitignored data directory, compute the SHA-256 of the raw bytes, and expose the HF revision recorded alongside it. Refuses to load a file whose hash does not match the manifest when one is present.
**Dependencies:** hashlib
**Inputs:** `path: Path`, optional `expected_sha256: str`
**Outputs:** `Dataset {questions: list[dict], sha256: str, revision: str}`; raises `DatasetIntegrityError` on mismatch

### COMP-004: TurnChunker
**Responsibility:** Split a session transcript into ingest chunks on turn boundaries only, targeting 1700 characters with a newline between every turn, and prefix each chunk with `[Session date: YYYY-MM-DD]`. Raises rather than truncates when a single turn alone exceeds the cap.
**Dependencies:** COMP-001
**Inputs:** `session: Session`, `max_event_chars: int`, `target: int = 1700`
**Outputs:** `list[Chunk {text, session_idx, chunk_idx, turn_ids}]`; raises `TurnExceedsCapError(turn_id)` when one turn > `max_event_chars`.

**Every chunk of a split session re-carries the date prefix, not only the first.** One chunk is fed as one agent turn, so each successive ingest turn re-inputs the session date and the date stays reachable at recall no matter which chunk a retrieval lands on. A session splitting into five chunks yields five dated chunks. The 1700 target (against a 2000 cap) is what leaves room for that repeated prefix plus NFKC expansion.

### COMP-005: SanitizeFidelityGate
**Responsibility:** The single highest-value safeguard in the harness. Runs every chunk through `arcmemory.security.sanitize` and `privacy_filter` locally before ingest, compares lengths, and when text was destroyed, checks whether the destroyed span overlaps a turn named by the question's gold-evidence flags. Gold overlap voids the question; non-gold shrinkage is recorded as a warning on the row.
**Dependencies:** COMP-004, arcmemory.security
**Inputs:** `chunk: Chunk`, `gold_turn_ids: set[str]`
**Outputs:** `FidelityVerdict {ok: bool, shrunk_by: int, gold_overlap: bool, spans: list[tuple[int,int]]}`; caller raises `GoldEvidenceFilteredError` on `gold_overlap`

### COMP-006: EvalAgentFactory
**Responsibility:** Build one throwaway `ArcAgent` per question by replicating `_load_arcagent` + `_scaffold_workspace` — `render_agent_config(...)` → scaffold → `load_config` → `ArcAgent(..., config_path=<abs>)` → `startup()` — rather than `arc agent create`, which mints identities into `~/.arcagent/keys`, signs capabilities and auto-registers over NATS. Emits the eval TOMLs with every knob this spec requires and asserts workspace containment before returning.
**Dependencies:** arcagent, COMP-016
**Inputs:** `question_id: str`, `run_dir: Path`, `tier='personal'`
**Outputs:** a started `ArcAgent` whose `_workspace` is asserted under `run_dir`; raises `WorkspaceEscapeError` otherwise.

TOML it emits: `[agent] name = 'lme-<question_id>'`; explicit workspace-relative `[security] policy_audit_log`; `ARCSTORE_DATA_DIR=<run_dir>/.arcstore`; `[modules.memory] brain='arcmemory'`, non-empty `distill_provider`, `consolidate_event_threshold`, `consolidate_idle_seconds`, `consolidate_interval_seconds`, and `[modules.memory.config.dynamics] consolidate_interval_minutes = 0.0` — all six together, because lowering the outer limit alone is a silent no-op; `top_k = 20`, `budget = 8000`; `[llm.modules.telemetry] store_raw_bodies = false`. `workpad` and `policy` modules are left ENABLED by operator decision.

### COMP-007: IngestDriver
**Responsibility:** Feed chunks to the agent in order via `run_collected(input_text, session_key=...)`, one distinct session key per chunk (`ingest:<session_idx>:<chunk_idx>`). Serial within a question. Never starts arcmemory's background consolidation loop, since the harness drives consolidation directly and arcmemory has no lock.
**Dependencies:** COMP-006, COMP-005, arcagent
**Inputs:** `agent: ArcAgent`, `chunks: list[Chunk]`
**Outputs:** `IngestReport {chunks_fed: int, chunk_hashes: list[str], warnings: list[str]}`; propagates `GoldEvidenceFilteredError` / `TurnExceedsCapError` to void the question

### COMP-008: ConsolidationWaiter
**Responsibility:** Fire and confirm one consolidation pass at each session boundary by awaiting the public `consolidate_poll_once()` directly — the module's `_CONSOLIDATE_POLL_INTERVAL = 300.0` is a constant, so per-session cadence is unreachable by config and this is the only reliable quiescence signal. Confirms the pass actually ran rather than trusting its return value alone.
**Dependencies:** arcagent.modules.memory.capabilities, COMP-006
**Inputs:** `agent: ArcAgent`, `workspace: Path`
**Outputs:** `ConsolidationResult {fired: bool, last_run_before, last_run_after, window_events: int}`; raises `ConsolidationStalledError` if `memory/.consolidate-last-run` did not advance or `.consolidate-manifest.json` remains. Attaches a WARNING-level handler to the `arcmemory.consolidate` logger for the whole run, because the arcmemory audit sink is null in a live agent and that logger is the only channel carrying `dedup_skipped` and degrade warnings.

### COMP-009: QueryRunner
**Responsibility:** Put the benchmark question to the same agent instance on a fresh session key, prefixed with `[Current date: YYYY-MM-DD]` from the question metadata — never wall-clock time, since the haystacks are historical and a run-year anchor fails the same 133 temporal questions a different way. Records the answer verbatim.
**Dependencies:** COMP-002, COMP-006
**Inputs:** `agent: ArcAgent`, `meta: QuestionMeta`
**Outputs:** `Answer {text: str, question_date_used: date, question_date_source: str, recalled_chunk_ids: list[str]}`

### COMP-010: JudgeAgent
**Responsibility:** Score one answer using a second, separate Arc agent running arcagent → arcrun → arcllm, pinned to model id `gpt-4o-2024-08-06`, `temperature = 0`, `max_tokens = 10`. Selects the prompt variant by question type: the standard grader, the refusal-checking prompt for `_abs` questions, and the rubric-grading prompt for `single-session-preference` (whose dataset `answer` field is a rubric, not a literal string). Constructed with its own key variable; never shares a client, config dict or message list with the system under test.
**Dependencies:** COMP-011, arcagent, arcllm
**Inputs:** `question: str`, `gold: str`, `answer: str`, `question_type: str`, `is_abstention: bool`
**Outputs:** `Verdict {correct: bool, raw_response: str, prompt_used: str, judge_model_id: str}`

### COMP-011: ReferencePromptVault
**Responsibility:** Hold a verbatim checked-in copy of the reference `evaluate_qa.py` prompt templates and expose them to the judge. Because the reference `print_qa_metrics.py` hard-asserts the judge string and cannot consume our output, prompt fidelity is the only remaining basis for comparability — so a test diffs the live prompts against this vault byte for byte.
**Dependencies:** _(none)_
**Inputs:** `question_type: str`, `is_abstention: bool`
**Outputs:** the exact prompt template string; the vault file itself is the fixture a `test_prompt_fidelity` diff test asserts against

### COMP-012: ScoringEngine
**Responsibility:** Aggregate verdicts into the three numbers the benchmark actually defines, and compute retrieval recall from the dataset's gold flags. Folds `_abs` results into their base question type for QA accuracy while excluding them from retrieval scoring entirely.
**Dependencies:** COMP-010, COMP-002
**Inputs:** `rows: list[ResultRow]`
**Outputs:** `Report {task_averaged_accuracy (macro over 6 types), overall_accuracy (micro), abstention_accuracy (separate), retrieval: {turn|session × recall_any@k, recall_all@k}, per_type: [{type, n, accuracy, wilson_95_ci, directional: bool}]}`. `directional = True` when a stratum holds fewer than 30 scored questions.

### COMP-013: JudgeAgreementSampler
**Responsibility:** Double-judge a fixed sample and record the agreement rate as a first-class metric, since `temperature = 0` does not make an LLM judge deterministic on borderline items. Preserves prior labels on the row whenever the judge prompt or model changes, so old and new can be diffed rather than silently overwritten.
**Dependencies:** COMP-010
**Inputs:** `rows: list[ResultRow]`, `sample_size: int`
**Outputs:** `AgreementReport {sample_size, agreement_rate, disagreements: list[question_id]}`; writes `prior_labels: list[Verdict]` onto affected rows

### COMP-014: RepoHygieneGuard
**Responsibility:** Own the `.gitignore` contract and enforce it at runtime. Ships the required patterns (dataset files, `runs/`, traces, audit artifacts, the three generated TOML names, results JSONL) plus repo-root guards `/workspace/`, `/traces/`, `/.audit/`, `/capabilities/` that catch the `ArcAgent(cfg)`-without-`config_path` leak even if the harness makes that mistake.
**Dependencies:** git
**Inputs:** `run_dir: Path`, `results_path: Path`
**Outputs:** `None` on success; raises `RepoHygieneError` when `git check-ignore -q` fails to match either path

### COMP-015: ArtifactScrubber
**Responsibility:** Pass every model-derived field — the agent answer, the judge raw response, and exception text, since provider error bodies echo request context — through `arcmemory.privacy_filter` before it reaches the results JSONL.
**Dependencies:** arcmemory.security
**Inputs:** `row: dict`
**Outputs:** the same dict with model-derived fields filtered in place

### COMP-016: RunManifest + provenance block
**Responsibility:** Assemble the provenance block stamped into every result row and write the standalone `run_manifest.json`. Redundancy is intentional: a single row must stay self-describing after rows from several runs are concatenated. Also the place where the two declared measurement deviations live in writing.
**Dependencies:** COMP-003, git
**Inputs:** resolved config, dataset, git state
**Outputs:** `Provenance {git_sha, git_dirty, harness_version, config_hash (sha256 of canonicalized RESOLVED config, since env overrides matter), dataset_sha256, dataset_revision, agent_model_id, judge_model_id, embedder_model_id, distiller_model_id, tier='personal', question_date_source, session_ingest_order, run_timestamp_utc, question_id}` plus `measurement_scope: {workpad_enabled: true, policy_enabled: true, note: 'accuracy measures arcmemory plus two additional system-prompt summarizers'}`

### COMP-017: PhaseRunner
**Responsibility:** The sequential outer loop. Runs exactly one question at a time with never more than one live agent, and gates the three phases — Oracle across all 500, then a stratified ~50-question sample of S covering all six types, then full S — behind an explicit flag per phase.
**Dependencies:** COMP-018, COMP-019, COMP-020, COMP-021, COMP-007, COMP-009, COMP-010
**Inputs:** `phase: Literal['oracle','sample-s','full-s']`, `questions: list[str]`
**Outputs:** streams completed `ResultRow`s to the ledger; returns a `PhaseReport`

### COMP-018: ResultLedger
**Responsibility:** Append-only JSONL with the question as the atomic unit of resume. Writes a row only after ingest, query and judge have all completed, carrying a terminal `"status": "complete"`. Builds the done-set once at startup and never seeks; anything missing, partial or unparseable counts as not done.
**Dependencies:** COMP-015, COMP-016
**Inputs:** `results_path: Path`
**Outputs:** `done_set() -> set[str]` (tolerates a truncated final line — the normal SIGKILL signature); `append(row) -> None` with per-line `flush()` and `fsync()` every N completions

### COMP-019: WorkspaceLifecycle
**Responsibility:** Create, mark and destroy each throwaway run directory. Writes `.ingest_complete` as the final ingest action so a resumed workspace lacking it is deleted and rebuilt rather than resumed mid-ingest. Teardown runs in a `finally` block and checkpoints SQLite WAL before deletion, because deleting the `.db` leaves `-wal`/`-shm` siblings that can exceed the main file.
**Dependencies:** sqlite3
**Inputs:** `run_dir: Path`, `keep_on_failure: bool`
**Outputs:** context manager; `mark_ingest_complete()`; `teardown()` runs `PRAGMA wal_checkpoint(TRUNCATE)` then removes the tree; refuses to start a phase when leftover workspaces exceed a configured threshold; retains the workspace for void/errored/disagreed questions under `--keep-workspace-on-failure`

### COMP-020: Preflight
**Responsibility:** Hard-fail the phase before any spend if a measurement seam is dead. Runs a live end-to-end pass in a scratch workspace through the real path — not a self-report — because both the embedder and the distiller have degraded silently in production before and `arc agent build --check` tests none of this.
**Dependencies:** COMP-006, COMP-008, COMP-014, COMP-003
**Inputs:** `phase config`
**Outputs:** `None` on success; raises `PreflightError` naming the failed assertion. Asserts: `_runtime.state().brain` is not a `NullBrain`; `brain._embedder`, `brain._distiller` and `brain._model` are all non-None after startup; one forced `consolidate_poll_once()` returns True; `.consolidate-last-run` advanced; the returned `episode_summary` reports `window_events > 0`; a `memory/daily-log/*.md` exists; the dataset SHA-256 matches the manifest; `git check-ignore -q` matches the run dir and results file; and the question date resolves from either the dataset field or the derived fallback.

**Also asserts the consolidation contract the harness is built on** (REQ-216): that `arcagent.modules.memory.capabilities._CONSOLIDATE_POLL_INTERVAL` still equals `300.0`, and that the eval agent's six consolidation settings hold the values this spec requires. This is an assumption guard, not a health check — the whole harness-drives-consolidation design exists *because* that constant is unreachable by config. If it silently changes to something small, the background loop starts firing alongside the harness's own passes, and arcmemory has no lock to make that safe.

### COMP-021: BudgetGovernor
**Responsibility:** Estimate before spending and abort when the estimate is exceeded. `--dry-run` walks the dataset through the REAL chunker (never `len(text)/4`) and multiplies by a versioned pricing table held in config, because at ~40,000 calls a 20-30% token-count error compounds into a meaningfully wrong ceiling. The running total persists so the ceiling survives a resume.
**Dependencies:** COMP-004, COMP-018
**Inputs:** `dataset`, `pricing_table_version`
**Outputs:** `Estimate {n_calls, tokens_in, tokens_out, cost_usd}`; `record(usage)` per question; raises `SpendCeilingExceeded` (aborts, never warns) at 110% of estimate; emits `{tokens_in, tokens_out, cost_usd, n_llm_calls, wall_seconds}` per question

### COMP-022: CLI
**Responsibility:** The single entry point. Plain `argparse`, no package, no install, no console-script entry — consistent with `evaluations/` staying outside the uv workspace.
**Dependencies:** COMP-017, COMP-020, COMP-021
**Inputs:** `--phase {oracle,sample-s,full-s}`, `--dry-run`, `--smoke N`, `--keep-workspace-on-failure`, `--resume`
**Outputs:** exit 0 on a clean phase; non-zero with the failed assertion named on preflight, hygiene or ceiling failure. `--smoke N` runs 3-5 questions spanning types end to end and gates entry to `--phase full-s`.


## Data Model

**No database and no schema migration.** The harness owns three on-disk artifacts, all gitignored.

**1. Result row (JSONL, one line per completed question).** Append-only; the question is the atomic unit.

| Field | Type | Note |
|---|---|---|
| `question_id` | str | primary key of the done-set |
| `status` | `complete` \| `void` \| `error` | terminal marker; anything else means not done |
| `void_reason` | `gold_evidence_filtered` \| `turn_exceeds_cap` \| null | excludes the row from accuracy scoring |
| `question_type` | str | one of the six |
| `is_abstention` | bool | `_abs` cross-tag, not a seventh type |
| `answer` | str | agent answer, scrubbed |
| `verdict` | object | `{correct, raw_response, prompt_used, judge_model_id}` |
| `prior_labels` | list | retained when judge prompt/model changes |
| `retrieval` | object | `{turn: {any@k, all@k}, session: {any@k, all@k}}`; null for `_abs` |
| `question_date` / `question_date_source` | date / `dataset`\|`derived` | REQ-214/215 |
| `cost` | object | `{tokens_in, tokens_out, cost_usd, n_llm_calls, wall_seconds}` |
| `provenance` | object | full COMP-016 block, redundantly per row |

**2. `run_manifest.json`** — one per run: the same provenance block, the pinned session ingest order, the dry-run estimate, the pricing-table version, and `measurement_scope` declaring that `workpad` and `policy` were enabled.

**3. Per-question workspace** (`evaluations/runs/<question_id>/`) — the agent's own tree: `memory/index.db` (+ WAL/SHM), `memory/daily-log/`, `.arcstore/`, `.audit/`, `traces/`, the three generated TOMLs, and `.ingest_complete` written last. Roughly 3-8MB each, so ~1.5-4GB across 500 if teardown ever stops running. Deleted on success; retained under `--keep-workspace-on-failure`.

**Types crossing the seam** are Pydantic models per CON-2: `Session`, `Turn`, `Chunk`, `QuestionMeta`, `FidelityVerdict`, `Verdict`, `ResultRow`, `Provenance`.

**Not modified:** arcmemory's `Event`, `Fact`, or index schema. The harness reads `Event` state only through public API. Note the write-path limitations it cannot fix without a framework change: all sessions bucket into one `daily-log/<today>.md`, every `Fact.date` is stamped today, and decay is inert because all `last_hit` values equal today.

## External Integrations

| System | Direction | Contract | Failure mode |
|---|---|---|---|
| **Arc stack** (arcagent → arcrun → arcllm → arcmemory) | harness → | Public API only: `render_agent_config`, `load_config`, `ArcAgent(..., config_path=)`, `startup()`, `run_collected()`, `consolidate_poll_once()`, `arcmemory.security.sanitize` / `privacy_filter` — every one already in its package's `__all__`. **Zero framework changes.** | A degraded seam (NullBrain, missing embedder, empty distiller) is caught by COMP-020 preflight, which aborts the phase. |
| **LongMemEval dataset** (HuggingFace `xiaowu0162/longmemeval-cleaned`) | → harness | Manual download into gitignored `evaluations/data/`. SHA-256 + HF revision pinned in the manifest and verified at preflight. | Hash mismatch raises `DatasetIntegrityError`. The Sept-2025 'cleaned' revision is NOT numerically comparable to the original, which is exactly why the hash is recorded. |
| **OpenAI, as judge** | harness → | Through arcagent → arcrun → arcllm per CON-3, pinned to `gpt-4o-2024-08-06`, `temperature=0`, `max_tokens=10`. `OPENAI_API_KEY` from the environment only, in its own variable, on its own agent. | Rate limit or failure marks the row `error`, not `complete`, so resume rebuilds it. |
| **Reference LongMemEval scripts** | → harness (offline) | `evaluate_qa.py` prompts are vendored verbatim into COMP-011 and diff-tested. `print_qa_metrics.py` is NOT used — it hard-asserts the judge string and cannot consume our output. | Prompt drift is caught by the fidelity diff test, which fails the build. |
| **git** | harness → | `git check-ignore -q` at preflight and phase start; `git rev-parse HEAD` plus dirty flag for provenance. | `RepoHygieneError` aborts before any artifact is written. |

**Deliberately NOT integrated:** NATS (the `messaging` and `tasks` modules stay off), the shared `~/.arc/store` (redirected per-run via `ARCSTORE_DATA_DIR`), and `arc agent create` (bypassed for the cheaper `render → scaffold → load → startup` path, avoiding identity minting into `~/.arcagent/keys` and NATS auto-registration).

## Traceability

| Requirement | Components |
|---|---|
| REQ-174 | COMP-001 |
| REQ-175 | COMP-002, COMP-003 |
| REQ-176 | COMP-004 |
| REQ-177 | COMP-004, COMP-007 |
| REQ-178 | COMP-002, COMP-004, COMP-016 |
| REQ-179 | COMP-006, COMP-007 |
| REQ-180 | COMP-005 |
| REQ-181 | COMP-005, COMP-007 |
| REQ-182 | COMP-008 |
| REQ-183 | COMP-006 |
| REQ-184 | COMP-007, COMP-008 |
| REQ-185 | COMP-006 |
| REQ-186 | COMP-006 |
| REQ-187 | COMP-009 |
| REQ-188 | COMP-010 |
| REQ-189 | COMP-010, COMP-011 |
| REQ-190 | COMP-010, COMP-012 |
| REQ-191 | COMP-010, COMP-012 |
| REQ-192 | COMP-012 |
| REQ-193 | COMP-012 |
| REQ-194 | COMP-013 |
| REQ-195 | COMP-014 |
| REQ-196 | COMP-014, COMP-020 |
| REQ-197 | COMP-015 |
| REQ-198 | COMP-006 |
| REQ-199 | COMP-006, COMP-016 |
| REQ-200 | COMP-016 |
| REQ-201 | COMP-003, COMP-016, COMP-020 |
| REQ-202 | COMP-017 |
| REQ-203 | COMP-017, COMP-022 |
| REQ-204 | COMP-018 |
| REQ-205 | COMP-019, COMP-007 |
| REQ-206 | COMP-018 |
| REQ-207 | COMP-020 |
| REQ-208 | COMP-021, COMP-022 |
| REQ-209 | COMP-021 |
| REQ-210 | COMP-016, COMP-018 |
| REQ-211 | COMP-019 |
| REQ-212 | COMP-021 |
| REQ-213 | COMP-019, COMP-022 |
| REQ-214 | COMP-002, COMP-009 |
| REQ-215 | COMP-002, COMP-009, COMP-016, COMP-020 |
| REQ-216 | COMP-020, COMP-008 |

## Alternatives Considered

**Home for the code.** Considered a standalone sibling repo (rejected: re-solves installing five fast-moving arc packages by hand for no benefit) and a `packages/arcbench/` package (rejected: puts a data-ingestion tool inside the framework's dependency DAG and buys quality gates plain scripts do not need). Chose `evaluations/` at the repo root — 'the framework must not know about it' is satisfied by direction of dependency, not physical distance. (D-494)

**Ingest path.** Considered calling `brain.capture()` directly (rejected by the operator despite being byte-identical to the hook at `capabilities.py:231`, and free of both LLM cost and synthetic commentary events), and considered adding a capture-only ingest entry or a `capture_respond=false` flag to arcagent (both withdrawn: framework changes). Chose a real `ArcAgent.run()` per chunk — the system must be exercised through the same surface a live agent uses. Costs accepted plainly: ~4,000 LLM calls on the sampled S run, ~40,000 on full S. (D-496)

**Ingest granularity.** Considered turn-by-turn replay (rejected: 5-10x the calls for granularity the session unit already preserves) and raising `max_event_chars` via `backend.dynamics` (rejected: chunking needs no config override at all). Considered leaving sessions unchunked (rejected: `sanitize()` silently truncates at 2000 chars, so most evidence turns would be cut and the run would measure the cap). (D-495)

**Source timestamps.** Considered threading a `ts` parameter through `Brain.capture` and `FastCapture.capture` (rejected: framework change) and skipping temporal-reasoning entirely (rejected: discards a whole capability the benchmark exists to measure). Chose the in-text date prefix, which is also what LongMemEval's own reference implementations do. Honest limitation carried into results: this is weaker than Zep's `reference_time=` or mem0's `timestamp=` first-class parameters, and the reference implementation's `temp_query_search_pruning.py` measures +7-11% temporal recall from proper date metadata that we cannot match. (D-498)

**Consolidation cadence.** Considered production cadence plus one final flush (rejected: the operator wants per-session distillation) and a single consolidation at the end (rejected: one window over 40 sessions is far outside the incremental regime the distill prompts were written for). Chose per-session with the harness driving `consolidate_poll_once()` directly — the config-only route is unreachable because the poll interval is a module constant. (D-499)

**Concurrency.** Considered bounded parallelism across questions with a semaphore sized to provider RPM (rejected by the operator). Sequential is slower in wall clock but deletes an entire class of shared-state hazards — the flocked WORM chain, the shared arcstore, rate-limit cascades, and `contextvars` isolation across sibling tasks — from the design.

**Judge.** Considered shelling out to the reference `evaluate_qa.py` verbatim (rejected by the operator: CON-3 requires LLM calls to flow through arcllm, and the operator is standing up a dedicated eval agent). Accepted consequence stated honestly: the reference `print_qa_metrics.py` cannot consume our output, so comparability now rests entirely on prompt fidelity — which is why COMP-011's byte-diff test is a Must, not a nicety.

**Module posture.** Considered disabling `workpad` and `policy` for cleaner attribution (rejected by the operator: it would deviate from D-496's premise that a real production agent is the product). Accepted consequence: the reported number covers arcmemory plus two additional summarizers, declared in the manifest.

**Workspace sharing.** Considered one shared workspace across all 500 haystacks (rejected: each instance is a different persona, so merging them makes consolidation fuse 500 contradictory identities and directly poisons the `knowledge-update` question type). (D-500)

## Risks and Mitigations

| Risk | Mitigation | Owner component |
|---|---|---|
| **The input filter silently eats gold evidence.** `_INJECTION_RE` deletes its match plus everything to end of line, and ordinary benchmark prose triggers it — 'Congratulations! You are now a certified PM as of May 2023…' becomes 'Congratulations!'. `privacy_filter` likewise mangles 'My secret: I actually hate cilantro'. | Local pre-check on every chunk; VOID the question on gold overlap rather than scoring it. The newline between turns confines end-of-line deletion to one turn. | COMP-005, COMP-004 |
| **A dead seam produces a real-looking number.** Empty `distill_provider` yields no consolidator; `embed_backend='none'` drops recall to BM25 + graph; a `dynamics` value failing pydantic validation degrades to `NullBrain` and the agent starts normally, because `configure_module_runtimes` is FAIL-OPEN. None of these raise. | Live end-to-end preflight through the real path, asserting object identity and observable side effects, aborting the phase on failure. | COMP-020 |
| **Cost blowout well past 'one call per chunk'.** `Consolidator.run()` uses an unbounded `TimeWindow()`, so every pass re-distills the entire episodic stream — O(n²) over 40 sessions. The agentic engine breaches `max_tokens=20_000` by roughly session 3, degrades, and pays for the full pipeline anyway. `resolve_entity` disambiguation is unbounded per fact candidate. Workpad adds ~2 more calls per question. | Real-chunker dry-run, a ceiling that ABORTS at 110% and survives resume, per-question cost telemetry with median-diff against the prior run, and phase gating so Oracle proves the harness before full S. | COMP-021, COMP-017 |
| **Comparability loss from the in-house judge.** We cannot run the reference metrics script. | Dated model id, `temperature=0`, `max_tokens=10`, byte-identical vendored prompts enforced by a diff test, judge-agreement rate logged, prior labels retained across judge changes. | COMP-010, COMP-011, COMP-013 |
| **Temporal confounds on both sides.** Write side: `Event.ts` is `now()`, so recency ranks by ingest order — accidentally correct on timestamp-sorted S, actively misleading on unsorted Oracle, which runs FIRST. All sessions collapse into one `daily-log/<today>.md` and every `Fact.date` is stamped today, degrading `knowledge-update` too. BM25 also tokenizes the date prefix uselessly and the graph channel cannot match a hyphenated date at all. | Read side is fixed by REQ-214/215. Write side is reported as a named limitation in the same sentence as the temporal number, never a footnote. A zero-cost ablation exists: `rebuild_index()` NULLs `mtime`, collapsing the recency list, so the query step can be re-run with and without it on an already-ingested workspace. | COMP-009, COMP-016, COMP-012 |
| **Artifacts or credentials entering the repo.** No `.gitignore` rule mentions `evaluations/` today; `store_raw_bodies` defaults to true and would write every haystack chunk as plaintext JSONL inside the repo tree; `ArcAgent(cfg)` without `config_path` resolves the workspace against the process CWD, which is the repo root. | Shipped ignore patterns plus repo-root guards, a `git check-ignore` hard-fail at phase start, `store_raw_bodies=false`, mandatory absolute `config_path` with a workspace-containment assertion, env-only keys, and `privacy_filter` over every model-derived artifact field including exception text. | COMP-014, COMP-006, COMP-015 |
| **Resume corruption.** A crash mid-ingest leaves a plausible-looking directory missing chunks and silently produces a worse answer. Chunk-level LLM calls are order-sensitive and not idempotent. | The question — not the chunk — is the atomic unit; `.ingest_complete` written last; any workspace lacking it is deleted and rebuilt; append-only ledger that tolerates a truncated final line. | COMP-018, COMP-019 |
| **Disk exhaustion from workspaces that are never thrown away.** ~3-8MB each × 500, and deleting the `.db` leaves `-wal`/`-shm` siblings that can exceed the main file. | Teardown in `finally` with `PRAGMA wal_checkpoint(TRUNCATE)`; refuse to start a phase when leftovers exceed a threshold. | COMP-019 |
| **Statistical over-claiming from the ~50-question phase.** Proportional stratification puts `single-session-preference` at n=3, where a Wilson interval is not stable; at n=13 and 77% observed the 95% CI spans roughly 50-92%. | Only pooled accuracy supports a confident claim at that phase; per-type numbers print with their CIs and a `directional` flag below n=30. Per-type claims are reserved for full S. | COMP-012 |
| **The consolidation assumption changes underneath us.** The harness drives `consolidate_poll_once()` itself precisely because `_CONSOLIDATE_POLL_INTERVAL = 300.0` is a module constant that config cannot reach. If a future arcagent change lowers it, the background loop begins firing alongside the harness's own passes — and arcmemory has no lock, so two passes interleave on the same SQLite connection and the same manifest. Nothing would raise; the run would just produce corrupted consolidation state. | Preflight asserts the constant still equals 300.0 and that the six eval consolidation settings hold their required values, erroring before the run rather than during it. | COMP-020, COMP-008 |
| **Blind spot: the harness cannot see arcmemory's own audit events.** `_runtime.configure` calls `select_brain(...)` without `audit_sink=`, so `_audit` falls back to `NullSink` and `memory.dedup_skipped`, `memory.consolidation_degraded`, `memory.fact_updated` all vanish in a live agent. | Attach a WARNING handler to the `arcmemory.consolidate` logger for the whole run — the only channel these surface on. Raise the missing `audit_sink=` wiring as a separate framework ticket; it is out of scope here. | COMP-008 |

## Open Questions

- Does the dataset ship a per-question date field, or does the derived fallback (latest `haystack_dates` entry) apply? A one-line check against the real file once it is downloaded. Either way the preflight asserts a date resolves and the row records which source was used, so this cannot silently regress — but the answer determines whether our temporal numbers are directly comparable to the reference implementation's.
- Can arcllm's SPEC-038 budget or circuit breaker trip mid-run and turn distillation into a silent no-op? Not traced. A long full-S run is exactly the workload that would trip it, and it would degrade the measurement without raising. Trace before the full-S phase, not before Oracle.
- Should `consolidate_engine = "pipeline"` be set in `dynamics` for the eval? It avoids paying for a doomed 20k-token agentic attempt before every pipeline pass, but it changes what is being measured, so it needs an explicit recorded call rather than a silent default.
- Should the sampled-S stratification weight the six question types evenly or match the natural distribution (multi-session 133, temporal-reasoning 133, knowledge-update 78, single-session-user 70, single-session-assistant 56, single-session-preference 30)? Deferred until Oracle results show which types are weakest.
- Framework ticket to raise separately, out of scope here: `select_brain(...)` is called without `audit_sink=`, so every arcmemory-internal audit event is discarded in a live agent. This is a latent observability bug that affects production, not just the eval.
