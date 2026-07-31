# Implementation Plan: SPEC-060 — Memory Ingestion & LongMemEval Evaluation

## Context References

- **PRD:** [PRD.md](./PRD.md)
- **SDD:** [SDD.md](./SDD.md)
- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Roadmap:** [.claude/steering/roadmap.md](../../steering/roadmap.md)

## Phase 1: Foundation

- [x] **T-783**: (red) Contract tests for ingest types and the SourceAdapter protocol
  - domain: test
  - Components: COMP-001
  - Requirements: REQ-174
  - Acceptance: Tests assert Session/Turn/Chunk are Pydantic models validating at the boundary (CON-2), and that a fake second adapter satisfies the protocol without importing anything from the longmemeval package. Fails because the module does not exist yet.
- [x] **T-784**: (green) SourceAdapter protocol and Session/Turn/Chunk models
  - domain: backend
  - Components: COMP-001
  - Requirements: REQ-174
  - Acceptance: evaluations/ingest/types.py and adapter.py exist and the preceding contract tests pass. An architecture test asserts evaluations/ingest/ imports nothing from evaluations/longmemeval/, and that nothing under packages/ imports evaluations/ — this is what keeps the dependency DAG untouched.
- [x] **T-785**: (red) TurnChunker tests: boundary split, target size, newline join, date prefix, oversized-turn raise
  - domain: test
  - Components: COMP-004
  - Requirements: REQ-176, REQ-177
  - Acceptance: Tests cover: never splits mid-turn; a newline separates every turn (which confines end-of-line filter damage to one turn); the 1700 target leaves margin because NFKC normalization can lengthen text AND because the date prefix repeats; and a single turn exceeding max_event_chars raises TurnExceedsCapError rather than truncating.
- [x] **T-824**: (red) Every chunk of a split session re-carries the session date
  - domain: test
  - Components: COMP-004
  - Requirements: REQ-176, REQ-178
  - Acceptance: A session long enough to split into five chunks produces five chunks EACH carrying [Session date: YYYY-MM-DD], not just the first. One chunk is fed as one ingest turn, so each successive turn re-inputs the date and the date stays reachable no matter which chunk a retrieval lands on. A companion test asserts the repeated prefix is counted against the 1700 target so a dated chunk can never exceed max_event_chars.
- [x] **T-786**: (green) TurnChunker implementation and the turn-length measurement
  - domain: backend
  - Components: COMP-004
  - Requirements: REQ-176, REQ-177, REQ-178
  - Acceptance: Chunker passes its tests and emits the [Session date: YYYY-MM-DD] prefix on every chunk. A measurement run reports the LongMemEval turn-length distribution and records whether any single turn exceeds max_event_chars — answering, before any spend, whether 'never split mid-turn' is achievable on this dataset at all.
- [x] **T-787**: (red) DatasetLoader tests: SHA-256 match, mismatch, revision capture
  - domain: test
  - Components: COMP-003
  - Requirements: REQ-175, REQ-201
  - Acceptance: A correct hash loads; a wrong hash raises DatasetIntegrityError; the HF revision is carried through. Uses a small committed fixture, never the real dataset.
- [x] **T-788**: (green) DatasetLoader implementation
  - domain: backend
  - Components: COMP-003
  - Requirements: REQ-175, REQ-201
  - Acceptance: Loads longmemeval_oracle.json and longmemeval_s_cleaned.json from the gitignored data directory and exposes sha256 plus revision for the manifest. The hash matters because the Sept-2025 'cleaned' revision is not numerically comparable to the original.
- [x] **T-789**: (green) Ship .gitignore patterns and repo-root guards
  - domain: infra
  - Components: COMP-014
  - Requirements: REQ-195
  - Acceptance: Patterns cover evaluations/data/, runs/, results/, traces and .audit, plus the three generated TOML names individually — never a blanket *.toml, which hides 41 tracked files (every pyproject.toml plus all five blueprint.toml). Root guards /workspace/, /traces/, /.audit/, /capabilities/ are present. A test asserts git check-ignore matches each artifact path AND that harness source is NOT ignored, since git cannot re-include a file beneath an ignored directory.
- [x] **T-790**: (green) RepoHygieneGuard: git check-ignore hard-fail
  - domain: backend
  - Components: COMP-014
  - Requirements: REQ-195, REQ-196
  - Acceptance: Raises RepoHygieneError when git check-ignore -q fails to match the run directory or the results file. A test proves that deliberately removing one pattern aborts the phase before any artifact is written.

## Phase 2: Core

- [x] **T-791**: (red) LongMemEvalAdapter tests: session ordering and question metadata
  - domain: test
  - Components: COMP-002
  - Requirements: REQ-175, REQ-178
  - Acceptance: Sessions yield in pinned dataset order and that order is reported for the manifest; QuestionMeta carries the question text, type, gold answer, answer_session_ids and has_answer turns. Fixtures pin one Oracle question and one S question, because Oracle haystacks are unsorted while S is timestamp-sorted.
- [x] **T-792**: (green) LongMemEvalAdapter session iteration
  - domain: backend
  - Components: COMP-002
  - Requirements: REQ-175, REQ-178
  - Acceptance: Maps one question's haystack into Session objects in dataset order and exposes question metadata. Ingest order is recorded, because the recency channel ranks by ingest order and without the record no two runs are comparable.
- [x] **T-793**: (green) Question-date resolution with the derived fallback
  - domain: backend
  - Components: COMP-002
  - Requirements: REQ-214, REQ-215
  - Acceptance: question_date resolves from the dataset field when present (source='dataset'), otherwise from the latest haystack_dates entry (source='derived'), and raises when neither is available. Settles against the real file whether the dataset ships a per-question date field, documented in the spec README either way.
- [x] **T-794**: (red) SanitizeFidelityGate tests against the LIVE arcmemory filters
  - domain: test
  - Components: COMP-005
  - Requirements: REQ-180, REQ-181
  - Acceptance: Tests import the real sanitize and privacy_filter, never mocks, and assert the known destructive cases: 'Congratulations! You are now a certified PM as of May 2023…' truncates to 'Congratulations!'; 'My secret: I actually hate cilantro' is redacted mid-sentence. A shrink overlapping a gold turn yields gold_overlap=True; a shrink on a non-gold turn yields a warning, not a void.
- [x] **T-795**: (green) SanitizeFidelityGate implementation and the dataset-wide damage report
  - domain: backend
  - Components: COMP-005
  - Requirements: REQ-180, REQ-181
  - Acceptance: Every chunk is checked locally before ingest and the caller voids the question with reason gold_evidence_filtered on overlap. A dataset-wide pass counts how often gold evidence shrinks and reports the rate BEFORE the Oracle run, because that rate decides whether any score is trustworthy at all. This is the single highest-value safeguard in the harness.
- [x] **T-796**: (green) EvalAgentFactory: throwaway agent construction
  - domain: infra
  - Components: COMP-006
  - Requirements: REQ-179, REQ-198
  - Acceptance: Replicates render_agent_config → scaffold → load_config → ArcAgent(config_path=<absolute>) → startup(), never `arc agent create`, which would mint identities into ~/.arcagent/keys, sign capabilities and auto-register over NATS. identity.did stays empty so the key mints lazily.
- [x] **T-797**: (red) Workspace containment assertion test
  - domain: test
  - Components: COMP-006
  - Requirements: REQ-198
  - Acceptance: Asserts agent._workspace resolves under the run directory and raises WorkspaceEscapeError otherwise. This is the guard against constructing ArcAgent without config_path, which resolves the workspace, traces, audit chain and capability scan root against the process CWD — the repo root.
- [x] **T-798**: (green) Memory config: the six consolidation knobs and the recall envelope
  - domain: infra
  - Components: COMP-006
  - Requirements: REQ-183, REQ-185
  - Acceptance: Emitted TOML sets brain='arcmemory', a non-empty distill_provider, consolidate_event_threshold, consolidate_idle_seconds, consolidate_interval_seconds AND dynamics.consolidate_interval_minutes = 0.0 together — a test reads the TOML back and asserts all six, because lowering the outer thresholds while the inner limit sits at 60 minutes is a silent no-op. Also sets top_k=20 and budget=8000, without which the run measures enforce_budget rather than memory.
- [x] **T-799**: (green) Per-question isolation, tier, and telemetry settings
  - domain: infra
  - Components: COMP-006
  - Requirements: REQ-186, REQ-199
  - Acceptance: Emits [agent] name='lme-<question_id>', a workspace-relative security.policy_audit_log, a per-run ARCSTORE_DATA_DIR, tier='personal', and store_raw_bodies=false — the last removing gigabytes of plaintext haystack text from inside the repo tree. workpad and policy modules are left ENABLED per operator decision. A test reads back every value.
- [x] **T-800**: (green) IngestDriver: serial per-chunk feed with per-chunk session keys
  - domain: ai-workflow
  - Components: COMP-007
  - Requirements: REQ-179, REQ-184, REQ-205
  - Acceptance: Feeds chunks in order via run_collected(input_text, session_key='ingest:<s>:<c>') after startup(). Tests assert distinct session keys per chunk — which is what removes quadratic prompt growth and the compaction call — and that the background consolidation loop is never started, since arcmemory has no lock anywhere and interleaved passes corrupt the shared manifest.
- [x] **T-801**: (green) ConsolidationWaiter: drive and confirm a pass per session boundary
  - domain: ai-workflow
  - Components: COMP-008
  - Requirements: REQ-182, REQ-184
  - Acceptance: Awaits the public consolidate_poll_once() directly, since the module's poll interval is a constant and per-session cadence is unreachable by config. Asserts memory/.consolidate-last-run advanced and .consolidate-manifest.json is absent, raising ConsolidationStalledError otherwise. Attaches a WARNING handler to the arcmemory.consolidate logger for the whole run — the only channel carrying dedup_skipped and degrade warnings, because the arcmemory audit sink is null in a live agent.
- [x] **T-802**: (green) QueryRunner: ask the question with the dataset's current date
  - domain: ai-workflow
  - Components: COMP-009
  - Requirements: REQ-187, REQ-214
  - Acceptance: Prefixes the question turn with [Current date: YYYY-MM-DD] from the question metadata on a fresh session key, and records the answer verbatim. A test asserts the date is the dataset's question date and never wall-clock time — Arc's system prompt carries no date at all, and a run-year anchor would fail the same 133 temporal questions a different way.
- [x] **T-803**: (green) Record which question-date source was used
  - domain: backend
  - Components: COMP-009
  - Requirements: REQ-215
  - Acceptance: Every answer carries question_date_used and question_date_source onto the result row, so a silently absent dataset field cannot masquerade as a memory failure in the temporal-reasoning numbers.
- [x] **T-804**: (green) ReferencePromptVault and the byte-diff fidelity test
  - domain: backend
  - Components: COMP-011
  - Requirements: REQ-189
  - Acceptance: Vendors the reference evaluate_qa.py prompt templates verbatim into the repo. A test diffs the live judge prompts against the vault byte for byte and fails the build on any drift. This is the only remaining basis for comparability, since print_qa_metrics.py hard-asserts the judge model string and cannot consume our output.
- [x] **T-805**: (green) JudgeAgent construction and model pinning
  - domain: ai-chain
  - Components: COMP-010
  - Requirements: REQ-188
  - Acceptance: A second, separate Arc agent runs arcagent → arcrun → arcllm per CON-3, pinned to model id gpt-4o-2024-08-06 (never the gpt-4o alias, which silently repoints), temperature=0, max_tokens=10, with its own key variable. A test asserts it shares no client, config dict or message list with the system under test.
- [x] **T-806**: (green) Judge prompt variant selection for abstention and preference questions
  - domain: ai-chain
  - Components: COMP-010
  - Requirements: REQ-190, REQ-191
  - Acceptance: Selects the standard grader, the refusal-checking prompt for _abs question ids, or the rubric prompt for single-session-preference. A test asserts the preference answer field is passed to the judge as a grading rubric and never string-matched, since fuzzy-matching it misscores roughly 6% of the benchmark.

## Phase 3: Integration

- [x] **T-807**: (green) ScoringEngine: the three accuracy numbers
  - domain: backend
  - Components: COMP-012
  - Requirements: REQ-190, REQ-191, REQ-192
  - Acceptance: Task-averaged accuracy is the macro mean over the six types; overall accuracy is the micro mean over all scored questions; abstention accuracy is reported separately. _abs rows fold into their base type for QA accuracy. Void rows are excluded from every accuracy figure and counted separately.
- [x] **T-808**: (green) Retrieval recall and Wilson confidence intervals
  - domain: backend
  - Components: COMP-012
  - Requirements: REQ-192, REQ-193
  - Acceptance: Reports recall_any@k and recall_all@k at both turn and session level with every k stated, excluding _abs rows entirely since answer_session_ids is meaningless for them. Wilson 95% CIs print beside every per-type accuracy, and strata below n=30 are flagged directional — at n=13 and 77% observed the interval spans roughly 50% to 92%.
- [x] **T-809**: (green) ArtifactScrubber over every model-derived field
  - domain: backend
  - Components: COMP-015
  - Requirements: REQ-197
  - Acceptance: Passes the agent answer, the judge raw response and exception text through arcmemory.privacy_filter before the row is written. A test asserts exception text is scrubbed too, because provider error bodies echo request context. A second test asserts no API key is ever read from a file — environment only.
- [x] **T-810**: (green) Per-row provenance block
  - domain: backend
  - Components: COMP-016
  - Requirements: REQ-210, REQ-199
  - Acceptance: Every row carries git_sha with a dirty flag, harness_version, config_hash over the canonicalized RESOLVED config (env overrides matter), dataset_sha256, all four model ids, tier, run_timestamp_utc and question_id — redundantly per row, so a single row stays self-describing after rows from several runs are concatenated.
- [x] **T-811**: (green) run_manifest.json with the declared measurement scope
  - domain: backend
  - Components: COMP-016
  - Requirements: REQ-200, REQ-178
  - Acceptance: Writes the manifest with the provenance block, the pinned session ingest order, the dry-run estimate and the pricing-table version. measurement_scope explicitly declares that workpad and policy were enabled and that the reported accuracy therefore covers arcmemory plus two additional system-prompt summarizers.
- [x] **T-812**: (green) Dataset integrity and date-source fields in the manifest
  - domain: backend
  - Components: COMP-016
  - Requirements: REQ-201, REQ-215
  - Acceptance: The manifest records dataset_sha256, the HF revision and question_date_source, and the loader refuses a dataset whose hash does not match. Closes the LLM04 data-poisoning surface on a third-party download feeding a memory store, and makes runs reproducible at the same time.
- [x] **T-813**: (green) WorkspaceLifecycle: ingest marker, WAL-safe teardown, leftover threshold
  - domain: infra
  - Components: COMP-019
  - Requirements: REQ-205, REQ-211, REQ-213
  - Acceptance: Writes .ingest_complete as the final ingest action; on resume a workspace lacking it is deleted and rebuilt, never resumed mid-ingest, because chunk-level LLM calls are order-sensitive and not idempotent. Teardown runs in a finally block, executes PRAGMA wal_checkpoint(TRUNCATE) then removes the whole tree, since deleting the .db leaves -wal/-shm siblings that can exceed the main file. Refuses to start a phase when leftovers exceed a threshold. --keep-workspace-on-failure retains void, errored and disagreed questions.
- [x] **T-814**: (green) ResultLedger: append-only JSONL with the question as the atomic unit
  - domain: backend
  - Components: COMP-018
  - Requirements: REQ-204, REQ-206, REQ-210
  - Acceptance: A row is written only after ingest, query and judge all complete, carrying terminal status='complete'. The done-set is built once at startup and the handle never seeks; a truncated final line loads cleanly, since that is the normal SIGKILL signature. Per-line flush, fsync every N completions. A test asserts a partial or unparseable row counts as not done.
- [x] **T-815**: (green) Preflight: live end-to-end seam assertions
  - domain: infra
  - Components: COMP-020
  - Requirements: REQ-207
  - Acceptance: In a scratch workspace, asserts through the REAL path that the brain is not a NullBrain; that _embedder, _distiller and _model are all non-None after startup; that one forced consolidate_poll_once() returns True; that .consolidate-last-run advanced; that episode_summary reports window_events > 0; and that a memory/daily-log/*.md exists. Tests prove it CATCHES a deliberately emptied distill_provider and a deliberately disabled embedder — both have degraded silently in production, and module configure failure is fail-open.
- [x] **T-825**: (green) Preflight guard on the consolidation contract
  - domain: infra
  - Components: COMP-020, COMP-008
  - Requirements: REQ-216
  - Acceptance: Before running, asserts that arcagent.modules.memory.capabilities._CONSOLIDATE_POLL_INTERVAL still equals 300.0 and that the eval agent's six consolidation settings hold their required values, returning an error if any differs. This is an assumption guard, not a health check: the harness drives consolidation itself precisely BECAUSE that constant is unreachable by config, and if it silently drops, the background loop fires alongside the harness's own passes with no lock to make that safe. A test monkeypatches the constant to a different value and asserts the preflight errors before any ingest.
- [x] **T-816**: (green) Preflight environment gates
  - domain: infra
  - Components: COMP-020
  - Requirements: REQ-196, REQ-201, REQ-215
  - Acceptance: Before any spend, the preflight verifies the dataset SHA-256, that git check-ignore matches the run directory and results file, and that a question date resolves from either the dataset field or the derived fallback. Any failure aborts the phase with the failed assertion named.
- [x] **T-817**: (green) BudgetGovernor: real-chunker dry-run estimate
  - domain: backend
  - Components: COMP-021
  - Requirements: REQ-208
  - Acceptance: --dry-run walks the dataset through the REAL chunker, never len(text)/4, and multiplies by a versioned pricing table held in config. At roughly 40,000 calls a 20-30% token-count error compounds into a meaningfully wrong ceiling, so a test asserts the estimate uses the production chunker path.
- [x] **T-818**: (green) Aborting spend ceiling and per-question cost telemetry
  - domain: backend
  - Components: COMP-021
  - Requirements: REQ-209, REQ-212
  - Acceptance: The ceiling ABORTS at 110% of the dry-run estimate rather than warning, and the running total persists so it survives a resume. Logs tokens_in, tokens_out, cost_usd, n_llm_calls and wall_seconds per question, so a median-cost diff against the prior run catches an ingest-cost regression before it burns the full-S budget. A test asserts the ceiling raises rather than logs.
- [x] **T-819**: (green) PhaseRunner: the strictly sequential, gated outer loop
  - domain: ai-workflow
  - Components: COMP-017
  - Requirements: REQ-202, REQ-203
  - Acceptance: Processes one question at a time with never more than one live ArcAgent — a test asserts no concurrent agent construction, which is what keeps the flocked WORM chain, the shared arcstore and rate-limit cascades entirely out of scope. Phases oracle → sample-s → full-s each require an explicit flag, and the ~50-question S sample covers all six types with its stratification recorded in the manifest.

## Phase 4: Polish

- [x] **T-820**: (green) CLI entry point with the smoke gate
  - domain: infra
  - Components: COMP-022
  - Requirements: REQ-203, REQ-208, REQ-213
  - Acceptance: Plain argparse, no package and no console-script entry, consistent with evaluations/ staying outside the uv workspace. Supports --phase, --dry-run, --smoke N, --keep-workspace-on-failure and --resume. --smoke runs 3-5 questions spanning types end to end and gates entry to --phase full-s.
- [x] **T-821**: (green) JudgeAgreementSampler: double-judge a fixed sample
  - domain: ai-chain
  - Components: COMP-013
  - Requirements: REQ-194
  - Acceptance: Double-judges a fixed sample and logs the agreement rate as a first-class metric, because temperature=0 does not make an LLM judge deterministic on borderline items. Retains prior_labels on rows whenever the judge prompt or model changes, so old and new labels can be diffed rather than silently overwritten.
- [x] **T-822**: (green) End-to-end smoke across three questions
  - domain: test
  - Components: COMP-005, COMP-007, COMP-008, COMP-017
  - Requirements: REQ-181, REQ-207
  - Acceptance: Runs 3 questions spanning types through ingest → consolidate → query → judge against the real stack, asserting a consolidation pass fired per session and that a gold-evidence void is produced when the fidelity gate is fed a deliberately filter-triggering chunk. Asserts git status is clean afterwards.
- [x] **T-823**: (green) Resume equivalence under SIGKILL
  - domain: test
  - Components: COMP-018, COMP-019, COMP-020
  - Requirements: REQ-204, REQ-205
  - Acceptance: SIGKILL the harness mid-ingest, resume, and assert the completed-question set matches an uninterrupted run, that the partial workspace was deleted and rebuilt rather than resumed, and that the truncated final ledger line loaded without error. This is the classic resume bug — treating workspace-exists as question-complete — proven absent.

## Traceability

| Requirement | Tasks |
|---|---|
| REQ-174 | T-783, T-784 |
| REQ-175 | T-787, T-788, T-791, T-792 |
| REQ-176 | T-785, T-824, T-786 |
| REQ-177 | T-785, T-786 |
| REQ-178 | T-824, T-786, T-791, T-792, T-811 |
| REQ-179 | T-796, T-800 |
| REQ-180 | T-794, T-795 |
| REQ-181 | T-794, T-795, T-822 |
| REQ-182 | T-801 |
| REQ-183 | T-798 |
| REQ-184 | T-800, T-801 |
| REQ-185 | T-798 |
| REQ-186 | T-799 |
| REQ-187 | T-802 |
| REQ-188 | T-805 |
| REQ-189 | T-804 |
| REQ-190 | T-806, T-807 |
| REQ-191 | T-806, T-807 |
| REQ-192 | T-807, T-808 |
| REQ-193 | T-808 |
| REQ-194 | T-821 |
| REQ-195 | T-789, T-790 |
| REQ-196 | T-790, T-816 |
| REQ-197 | T-809 |
| REQ-198 | T-796, T-797 |
| REQ-199 | T-799, T-810 |
| REQ-200 | T-811 |
| REQ-201 | T-787, T-788, T-812, T-816 |
| REQ-202 | T-819 |
| REQ-203 | T-819, T-820 |
| REQ-204 | T-814, T-823 |
| REQ-205 | T-800, T-813, T-823 |
| REQ-206 | T-814 |
| REQ-207 | T-815, T-822 |
| REQ-208 | T-817, T-820 |
| REQ-209 | T-818 |
| REQ-210 | T-810, T-814 |
| REQ-211 | T-813 |
| REQ-212 | T-818 |
| REQ-213 | T-813, T-820 |
| REQ-214 | T-793, T-802 |
| REQ-215 | T-793, T-803, T-812, T-816 |
| REQ-216 | T-825 |

## Open Questions

- Settled during implementation against the real file: does the dataset ship a per-question date field, or does the derived fallback apply? The preflight asserts a date resolves either way, so this cannot silently regress — but the answer determines whether our temporal numbers are directly comparable to the reference implementation's.
- Trace before the full-S phase, not before Oracle: can arcllm's SPEC-038 budget or circuit breaker trip mid-run and turn distillation into a silent no-op? A long full-S run is exactly the workload that would trip it, and it would degrade the measurement without raising.
- Needs an explicit recorded call: set consolidate_engine = 'pipeline' in dynamics? It avoids paying for a doomed 20k-token agentic attempt before every pipeline pass, but it changes what is being measured, so it must not be a silent default.
- Deferred until Oracle results show which types are weakest: should the sampled-S stratification weight the six types evenly or match the natural distribution (multi-session 133, temporal-reasoning 133, knowledge-update 78, single-session-user 70, single-session-assistant 56, single-session-preference 30)? Proportional sampling puts single-session-preference at n=3.
- Out of scope here, raise as a separate framework ticket: select_brain(...) is called without audit_sink=, so every arcmemory-internal audit event is discarded in a live agent. That is a production observability bug, not an eval bug.
