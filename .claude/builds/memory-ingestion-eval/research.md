# memory-ingestion-eval — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-492–D-500 (9 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Memory Ingestion & LongMemEval Evaluation — Build Decisions (2026-07-30)

**Phase**: build | **Status**: complete | **Total decisions**: 9 (7 user, 2 auto-applied)
**ID range**: D-492 to D-500
**Priority framework**: simplicity → modularity → security → scalability

#### Summary
An evaluations/ folder in the arc repo that bulk-ingests outside data through the existing, unmodified Arc agent + arcmemory stack, then measures memory quality against LongMemEval. A single ingest pathway with pluggable source adapters (longmemeval first; email and Slack to follow) feeds sessions to a real ArcAgent turn-by-chunk; arcmemory's own distill prompts and retrieval are what is under test. Zero framework changes.

#### Research Insights

**From Solutions Archive:**
- MEMORY INDEX (project) — `arcmemory embedder silent-degrade`: a missing sentence-transformers install made dedup a silent no-op. Same failure class the D-500 preflight must catch; degrade LOUD.
- MEMORY INDEX (project) — `arcmemory distiller unwired`: `distill_provider` was unset in blueprints and scaffold, so consolidation was dead fleet-wide. This is the exact seam D-500's preflight exists for.
- MEMORY INDEX (project) — `Producers-unwired pattern`: Arc specs ship correct predicates with dead activating wiring. Demand an E2E-through-the-real-path assertion, never a self-report.

**Best Practices:**
- Pull `longmemeval_s_cleaned.json` / `longmemeval_oracle.json` explicitly and record the dataset sha256 + HF revision in every result row — the Sept-2025 'cleaned' revision changed session content to reduce cross-question interference and is NOT comparable to the original.
- Report three numbers the way the benchmark defines them: Task-averaged Accuracy (macro mean of the six per-type accuracies), Overall Accuracy (micro over all 500), and Abstention Accuracy (the 30 `_abs` questions, separately).
- State the reading strategy explicitly in results. The paper's Figure 6 shows a ~15-point QA-accuracy swing between 'NL + Direct' and 'JSON + Chain-of-Note' under ORACLE retrieval — same evidence, same reader. Prompt format alone moves the headline number more than most memory improvements will.

**Edge Cases:**
- `single-session-preference`'s `answer` field is a grading RUBRIC, not a literal answer string. Fuzzy-matching it silently misscores ~6% of the benchmark.
- Abstention (`_abs` id suffix, 30 questions) is not a 7th `question_type` — it is a cross-tag on the existing six. It uses a refusal-checking judge prompt, is folded into its base type for QA accuracy, and is EXCLUDED from retrieval scoring (`answer_session_ids`/`has_answer` are meaningless for it).
- Known open gold-label defects in the cleaned dataset: relative-date resolution off by a week and a Valentine's-Day session-date mismatch (issue #50), plus annotation-error reports #37-#41. Spot-check rather than assuming labels are clean.
- Session-count sources disagree: the repo README says ~40 history sessions for LongMemEval-S, the paper body says ~50. The ~115k-token figure is confirmed by both. A direct measurement in the first Oracle run settles it.

**Performance:**
- LongMemEval-S: ~115k tokens of history per question x 500 questions. LongMemEval-M: ~1.5M tokens per question, deliberately too large for direct long-context reading. Measured average over the first 50 S questions: ~49 sessions (range 41-57).
- The GPT-4o judge is a second cost layer on top of ingest and answering — 500 judge calls per phase, single-threaded in the reference script.

**References:**
- https://arxiv.org/abs/2410.10813 — LongMemEval (ICLR 2025)
- https://github.com/xiaowu0162/LongMemEval
- https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
- https://github.com/xiaowu0162/LongMemEval-V2 — agentic-context successor, closer to Arc's actual use case
- packages/arcmemory/README.md:278,374 — arcmemory's own 'no published benchmark yet' status

#### Auto-Applied (Compliance Mandates)
| ID | Category | Decision | Mandated Answer | Citation |
|---|---|---|---|---|

#### Research Insights

**From Solutions Archive:**
- .claude/solutions/security-issues/2026-04-18-ast-validator-is-not-enough-defense-in-depth.md — 'static validation is inherently incomplete.' `_INJECTION_RE` is seven literal English phrasings; it is exactly the single-layer static filter that solution warns against, and D-493 currently treats it as sufficient.
- .claude/solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md — injection-prevention and unicode-normalization patterns from the scheduler review apply verbatim to this ingest path.
- MEMORY INDEX (project) — `Trifecta is a context-resolved 3-leg model (SPEC-057)`: never bypass the gate. The finding below is that on a default agent the gate is not bypassed — it is simply never reached.

**Best Practices:**
- Give the ingest agent ZERO tools. Its job is 'read a chunk, say something short.' Set an explicit `[tools.policy] deny` list covering every builtin and module tool, `allowed_paths = []`, `egress_allowlist = []`, and belt-and-braces `[sandbox] allowed_tools = []` with `max_turns = 2` in `arcrun.toml`. Make the HumanGate unreachable by construction rather than tuning it.
- Disable `[modules.workpad]` and `[modules.policy]` for the eval. Both write LLM-derived text into the SYSTEM PROMPT every turn through a sanitizer with no injection filtering. This is a measurement decision as much as a security one: left on, the run measures arcmemory plus two other summarizers.
- Set `[tools.human_gate] timeout_seconds = 1.0` and `auto_approve = []`. A live approval channel is wired by default, so an unattended trifecta block POLLS for 300 seconds before denying. Never use `auto_approve` — the config permits listing the whole trifecta, which is precisely the wrong knob.
- Isolate the shared state: `ARCSTORE_DATA_DIR=<run_dir>/.arcstore` (env has highest precedence) or `[arcstore] enabled = false`; a unique `[agent] name` per question; an explicit workspace-relative `[security] policy_audit_log`.
- Set `[llm.modules.telemetry] store_raw_bodies = false` for the eval agent — it removes the largest plaintext artifact and gigabytes of disk.
- Run at `tier = "personal"`. Enterprise forces `custody = "vault_transit"`, adding a network round-trip per audit record; at personal tier signing is `in_process` (~50us x 40k ~= 2 seconds total). This is the one place where the lower tier is the right call — BECAUSE the zero-tool posture above removes what personal tier fails to fence.
- Keep `WormSink` (the default audit sink). Swapping to `NullSink` would be a framework change, and an eval that disables the audit chain no longer exercises the production surface it claims to measure. With no tools, the chain is nearly empty anyway.
- Checksum the dataset (SHA-256 in the run manifest, verified at preflight) and scrub every model-derived artifact field through `arcmemory.privacy_filter` before it reaches the results JSONL — including exception text, since provider error bodies echo request context.

**Edge Cases:**
- GITIGNORE — verified NOT IGNORED today: `evaluations/longmemeval/data/longmemeval_s.json`, `evaluations/runs/q1/workspace/memory/index.db`, `evaluations/runs/q1/traces/traces-*.jsonl`, `evaluations/results/run.jsonl`, `evaluations/runs/q1/.audit/trace-checkpoint.worm`, `evaluations/runs/q1/arcagent.toml`. Report only, not edited.
- GITIGNORE GOTCHA — git cannot re-include a file under an IGNORED DIRECTORY, so a broad `evaluations/**/runs/` permanently swallows anything beneath it and a `!` negation inside will not work. Keep harness code out of `runs/`. Also avoid a blanket `evaluations/**/*.toml` (it would swallow a checked-in template) — name `arcagent.toml`/`arcllm.toml`/`arcrun.toml` individually and keep `*.toml.example`. Note the existing `architecture/` and `**/architecture/` rules will silently swallow an `evaluations/architecture/` if one is ever created.
- ADD A REPO-ROOT GUARD to `.gitignore` (`/workspace/`, `/traces/`, `/.audit/`, `/capabilities/`) against the `ArcAgent(cfg)`-without-`config_path` leak, so the mistake is caught even if the harness makes it.
- `privacy_filter`'s six patterns cover OpenAI/GitHub/Slack/AWS/PEM/`key: value` but MISS `sk-ant-` (Anthropic), `AIza` (Google) and JWTs — patterns that exist in `arcllm/_secrets.py` but which arcmemory does not use. There is no entropy tier, so a bare unprefixed key is caught by neither.
- The secrets story is otherwise clean and D-492 is satisfied as shipped: `resolve_api_key()` goes vault -> env -> raise, the vault-fallback warning logs NAMES only, and the scaffold stores env-var names rather than values. One undocumented third source exists: `~/.arc/secrets/{name}` with 0600 enforced.
- Audit events are clean — `AuditEvent` carries `payload_hash` only, and `_emit_drop` explicitly hashes. But `_redact_sensitive` masks by KEY NAME only, so a secret sitting in a value under a benign key passes.
- `memory.captured` audit events are DISCARDED in the default wiring (`select_brain` is called without `audit_sink`), so `arcmemory/capture.py:10`'s claim that memory writes are audited does not hold in a real agent. A framework gap, not an eval problem — but it means the write path is unaudited during the run.
- An injected `schedule_create` would execute INSIDE the live run (min interval 60s, up to 50 schedules), and `messaging_send` to a third-party handle is one of the very few calls that WOULD trip the gate. Both vanish with the deny list.

**Performance:**
- Signed-chain cost is a non-issue at personal tier: one SHA-256 plus one Ed25519 sign per record, ~50us each, so 40k records is about 2 seconds. Rotation is fine (max_records 100_000, max_bytes 50MB). At enterprise `custody = "vault_transit"` the same 40k becomes 40k network round-trips.
- The real audit-adjacent cost is trace disk: 40k calls x (recall block + up to 2000-char chunk + response) with bodies on by default is conservatively several GB of plaintext JSONL.
- Telemetry volume: `memory.capture` and `memory.recall` emit ~2 log lines per chunk, so roughly 80k lines on a full-S run. Volume, not integrity.

**References:**
- packages/arcmemory/src/arcmemory/security.py:25-29, 33-43, 47-54, 58-82, 199-233, 236-250, 288-290
- packages/arcagent/src/arcagent/core/agent_dispatch.py:112-114, 132
- packages/arcagent/src/arcagent/utils/sanitizer.py:42-58 — no injection filtering on the system-prompt path
- packages/arcagent/src/arcagent/core/session_internal/context.py:45, 52, 57-59, 239-245
- packages/arcagent/src/arcagent/core/session_internal/capability_ledger.py:68-95, 113, 119
- packages/arcagent/src/arcagent/builtins/capabilities/bash.py:34-45; _runtime.py:533-538
- packages/arcagent/src/arcagent/core/agent.py:105-112, 198-209, 467
- packages/arcllm/src/arcllm/config.toml:29-33, 42-47; vault.py:110-157
- packages/arctrust/src/arctrust/audit.py:138-143, 159-170, 186-189, 220-221
- packages/arccli/src/arccli/commands/agent/_common.py:96-551 — the default tool and module inventory






#### Research Insights

**From Solutions Archive:**
- .claude/solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md — injection-prevention, unicode-normalization and unbounded-resource-consumption patterns; the same `sanitize()` surface this harness feeds.
- .claude/solutions/security-issues/2026-02-21-arcrun-phase4-hardening-review-learnings.md — async/sync bridge, thread-safety and cleanup patterns for a long-running agent loop; directly applicable to per-question workspace teardown.
- MEMORY INDEX (project) — `contextvars sibling-task regression (build/bind split)`: build once at startup, `bind()` at the top of every turn-dispatch entry. Confirms the safe concurrency shape below.

**Best Practices:**
- Chunk at ~1700 chars, not 2000, and put a `\n` between every turn. The newline is a real defense: `_INJECTION_RE` destroys to end-of-line, so a newline confines the damage to one turn instead of the rest of the chunk. The margin absorbs NFKC expansion (normalization can LENGTHEN text) and the `[Session date: …]` prefix.
- Build a sanitize-fidelity gate in the adapter: import `sanitize` and `privacy_filter` from `arcmemory.security` (both are in `__all__`, no framework change), run every chunk through them locally before ingest, and assert output length ~= input length. Cross-check every shrink against that question's `answer_session_ids` / `has_answer` turns. If gold evidence was eaten, the question's result is VOID, not a memory failure. This is the single highest-value thing to build.
- Use one session key per chunk (`ingest:<session_idx>:<chunk_idx>`) and a separate key for the question turn. Memory scope is `agent_did`-only — `_capture` passes no `session_id` (capabilities.py:231) — so this costs nothing in recall and removes quadratic prompt growth and compaction entirely.
- Raise the recall envelope in the eval TOML (`top_k = 20`, `budget = 8000`) or the run measures `enforce_budget` rather than memory.
- Disable in the eval TOML: `[spawn]`, `[modules.workpad]`, `[modules.policy]`, `[modules.scheduler]`, `[modules.messaging]`, `[modules.tasks]`, `[modules.runcontrol]`, `[modules.skills]`, `[arcstore]`, telemetry trace export. Leave `[modules.memory]` and `[modules.memory_acl]` alone — those ARE the system under test.
- Give every question a unique `agent.name` (e.g. `lme-<question_id>`) AND an explicit workspace-relative `security.policy_audit_log`, or concurrent agents deadlock on the shared WORM chain.
- Concurrency shape: one `asyncio.Task` per question via `asyncio.gather` over `run_collected(...)` coroutines. NEVER manually interleave `agent.run()` async generators in a single task — async generators do not get isolated contexts in CPython, so `activate_runtime_bindings` from one clobbers the other mid-iteration.
- Bound the pool. Guard every provider call with an `asyncio.Semaphore(N)` sized against the actual tier's RPM, retry with capped exponential backoff plus jitter, honour `Retry-After`, and raise a distinct exception class for rate-limit vs hard failure. Unbounded `asyncio.gather` over 500 questions has no backpressure and turns one 429 into a cascade.
- Parallelize ACROSS questions, keep chunk ingestion SERIAL within a question — ordering matters to distillation and it keeps reproducibility simple.
- `ArcAgent.run()` is an async generator requiring a `SessionManager`, not a string. Use `run_collected(input_text, session_key=...)`; `startup()` must have run first.
- Cheapest correct throwaway construction is NOT `arc agent create` (it mints identities into `~/.arcagent/keys`, signs capabilities, and auto-registers over NATS). Replicate `_load_arcagent` + `_scaffold_workspace`: `render_agent_config(...)` -> scaffold -> `load_config` -> `ArcAgent(...)` -> `startup()`. Leave `identity.did = ""` so the key mints lazily.
- Recall does NOT require consolidation — `iter_source_chunks` indexes every raw episodic event 1:1 (index/source.py:42-75), so BM25/vector/graph recall works from turn one. Consolidation is what is under test, not what makes recall possible.

**Edge Cases:**
- `_INJECTION_RE` (arcmemory/security.py:33-43) deletes the match and `[^\n]*` after it. `"Congratulations! You are now a certified PM as of May 2023, and your ID is 88231."` becomes `"Congratulations!"`. `"I told my boss to forget everything about the old plan…"` becomes `"I told my boss to"`. Both verified against the live regex.
- `privacy_filter` (security.py:52) redacts `password|passwd|secret|api[_-]?key|token\s*[:=]\s*\S+`. Ordinary prose triggers it: `"My secret: I actually hate cilantro"` -> `"My [REDACTED] actually hate cilantro"`. It does NOT touch names, emails, phones or addresses — verified byte-identical passthrough — so most gold evidence survives.
- `sanitize()` hard-truncates at `max_length` with no ellipsis, no word boundary, no warning and no return signal (security.py:73). NFKC normalization runs FIRST and can expand length, so a 2000-char raw chunk can exceed 2000 post-normalize.
- `capture_respond` stores `chunk_text + "\n" + reply`, not the reply (capabilities.py:220-221, agent_dispatch.py:333-340). Under 2000 chars: every evidence chunk is stored twice and distillation sees it doubled. At/over 2000 chars: the joined text truncates back to the `user` event byte-for-byte, the Deduper drops it, and the reply is never stored at all — chunking exactly at the cap eliminates D-496's commentary-noise concern by accident.
- Dedup is exact SHA-256 equality over the last 128 captures (security.py:90-112), NOT similarity. Near-duplicate chunks from different sessions are never dropped. Non-risk for this workload.
- An oversized single turn is silently truncated and undetectable through the public path: `_capture` discards `capture()`'s return value (capabilities.py:231). Whether LongMemEval contains turns >2000 chars is UNMEASURED — the adapter must measure it before the Oracle run, because it decides whether D-495's 'never split mid-turn' is achievable at all.
- WORM flock: `_policy_audit_log_path` slugs `agent.name` into a SHARED `resolve_data_dir()/worm/audit-chain-<slug>.jsonl` and `WormSink` takes `flock(LOCK_EX|LOCK_NB)`, raising `RuntimeError: another writer holds …` (core/agent.py:183-209, arctrust/audit.py:238-250).
- `[arcstore] data_dir = ""` in the scaffold template points every agent at one shared `~/.arc/store` SQLite; `modules.messaging` and `modules.tasks` both dial `nats://127.0.0.1:4222` at startup. 500 agents = 500 connection attempts plus degrade timeouts.
- `Event` has no `source`, `role` or `origin` field. The ONLY discriminator between ingested benchmark text and the agent's own commentary is `kind`, and `curate_conversation_kinds` feeds BOTH `user` and `respond` to distillation — so nothing downstream can filter the commentary out.

**Performance:**
- Recall budget is the binding constraint: `top_k=5` but `budget=1024` tokens (modules/memory/config.py:41-42). A 2000-char event is ~525 tokens and `enforce_budget` counts the boundary-marked block, so exactly ONE 2000-char recall survives regardless of `top_k`.
- One session key for all 40 chunks re-sends the full history every turn (agent_dispatch.py:277) — quadratic prompt growth plus a compaction LLM call at the 0.85 threshold. Per-chunk session keys remove both.
- `[spawn] enabled = true` is the template default and costs a SECOND full `assemble_system_prompt` per turn.
- Two `brain.retrieve()` calls per turn by default — memory recall at `assemble_prompt`, plus the skills-improver `inject_insight` hook at `pre_respond`.
- Workpad's `context.md` rewrite fires `every_n_runs = 20`, so a 40-chunk loop triggers two extra background LLM calls per question before any of the eval's own cost.
- Benign shared state: arcllm's `_local_cache` loads the local embedding model once under a `threading.Lock` and encodes via `asyncio.to_thread` — parallel questions contend on CPU, not correctness.

**References:**
- packages/arcmemory/src/arcmemory/security.py:33-43, 47-54, 58-74, 90-112
- packages/arcmemory/src/arcmemory/capture.py:60, 64-71, 76, 80-89
- packages/arcagent/src/arcagent/modules/memory/capabilities.py:183-234
- packages/arcagent/src/arcagent/core/agent_dispatch.py:234-341
- packages/arcagent/src/arcagent/core/agent.py:183-209, 612-699
- packages/arcagent/src/arcagent/modules/memory/_runtime.py:96-99, 154-172
- packages/arctrust/src/arctrust/audit.py:238-250
- tests/architecture/test_no_module_global_agent_state.py — AST gate that fails the build on `global` reassignment in a `_runtime.py`



#### Research Insights

**From Solutions Archive:**
- .claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md — a value that is constant for a run but consumed at many call sites must flow through construction. The source timestamp is exactly that value, and the fact that it cannot flow is what D-498 is working around.
- MEMORY INDEX (project) — `Trifecta is a context-resolved 3-leg model` and `arcmemory architecture`: structural/analogical retrieval is time-blind by design, which is why the recency channel carries disproportionate weight here.

**Best Practices:**
- Pin and RECORD the adapter's ingest order per run. The entire recency analysis depends on it, and LongMemEval-S is timestamp-sorted while Oracle is not — without the record, arms of any later ablation are not comparable.
- The LongMemEval reference keeps `haystack_dates` as a parallel metadata array and ships `src/index_expansion/temp_query_search_pruning.py`, which infers a time range from the query and FILTERS candidates. The authors measure +7-11% temporal recall from it. Zep's `add_episode(reference_time=)` and mem0's `client.add(timestamp=)` are both first-class parameters, explicitly motivated by data migration and consistent chronology.
- Report the temporal-reasoning number with the confound named in the same sentence, not in a footnote. Zep's advantage over mem0 on this exact sub-task is ~15 points and is attributed to timestamp modelling.

**Edge Cases:**
- CORRECTION to D-498's stated consequence: `datetime.now(UTC).isoformat()` has microsecond resolution, so the ~40 sessions share a wall-clock DAY, not a timestamp. Every event is strictly ordered — the recency channel carries a full, perfectly-ordered signal that happens to equal INGEST ORDER. 'Recency ranking has no signal' is false; the accurate statement is that recency ranks by ingest order.
- On LongMemEval-S/M, `haystack_session_ids` are timestamp-sorted, so ingest order = chronological and the recency channel is ACCIDENTALLY CORRECT — any temporal score partly rides an artifact you did not design. On Oracle, sessions are explicitly unsorted, so recency hands up to 25% of max score to an arbitrary handful of sessions. D-500 runs Oracle first.
- Write-path damage D-498 does not mention: `by_day[event.ts[:10]]` (consolidate.py:394) collapses all 40 sessions into ONE `memory/daily-log/<today>.md`; the day-summary prompt explicitly demands an HH:MM chronological timeline and is handed the ingest wall clock; `Fact.date` defaults to today (types.py:92), so the `| was:` contradiction trail loses date discrimination — that hits `knowledge-update`, not just `temporal-reasoning`.
- Decay is inert: all `last_hit` = today, so `elapsed_days ~= 0`, `exp(0) = 1`, and `edges_decayed` is always 0. The forgetting path is untested in every eval run and that metric is meaningless.
- BM25 tokenizer mismatch: `fts5` indexes `[Session date: 2023-05-14]` as `session/date/2023/05/14` (default unicode61), but the QUERY tokenizer strips non-alphanumerics WITHIN a word, so a query containing `2023-05-14` becomes the single term `20230514` — a token that does not exist in the index. And since every chunk carries a date prefix, `2023`/`05`/`14` have near-zero IDF anyway.
- Graph channel is effectively unreachable by a date: `_phrase_regex` rewrites `-` to a space, so a slug `2023-05-14` compiles to `\b2023\ 05\ 14\b` and will not match the hyphenated text. The structural/analogical channel has no ts or date handling at all.
- `rebuild_index()` deliberately writes `mtime = None` (index/rebuild.py:127-128,144), and `index_if_needed` only rewrites content-hash-changed chunks — so after any rebuild the recency list silently collapses to a lexicographic `chunk_id` ordering and stays that way.
- `Event` has no metadata dict, no tags and no note field. `refs: list[str]` is persisted and re-hydrated but never written and never read — a dead field, and not settable through `capture()` anyway. `kind` IS free-form and settable, but a custom kind is silently dropped from distillation unless `curate_conversation_kinds` is also overridden.

**Performance:**
- Recency is an unweighted 4th list in RRF: `[bm25, graph, recency]` plus `vec` when available, fused at `1/(60+rank)` with no weights (index/surface.py:178-182, fusion.py:13,29). Max single-list contribution 1/60 = 0.01667; max total 0.0667. The recency list alone can supply 25% of the maximum achievable score, and a rank-0-by-recency chunk gets exactly as much as the top BM25 hit.
- `_recency_order()` is an UNBOUNDED `SELECT … ORDER BY COALESCE(mtime,0) DESC` over every chunk in the scope. With ~500 chunks per haystack (250 event chunks, doubled by D-496's per-chunk commentary), contributions span 1/60 down to 1/559 — a ~9x spread, but concentrated: only the ~60 most recently ingested chunks (roughly the last 5 sessions) get a boost comparable to a strong lexical hit.
- The embedder embeds the WHOLE chunk as one vector, so a ~25-char date prefix inside a 2000-char chunk is ~1% of the token mass — and dense embeddings are weak at exact date discrimination regardless. Expect near-zero contribution from the vec channel.

**References:**
- packages/arcmemory/src/arcmemory/types.py:62-79, 92, 112, 212-216
- packages/arcmemory/src/arcmemory/index/surface.py:100-117, 159-164, 178-182, 240-247, 300-308
- packages/arcmemory/src/arcmemory/index/rebuild.py:127-128, 144
- packages/arcmemory/src/arcmemory/index/graph.py:83, 107, 185-218
- packages/arcmemory/src/arcmemory/consolidate.py:187, 394
- https://github.com/xiaowu0162/LongMemEval — `src/index_expansion/temp_query_search_pruning.py`
- https://arxiv.org/abs/2501.13956 — Zep temporal knowledge graph (temporal-reasoning 62.4% vs 45.1% full-context)
- https://docs.mem0.ai/platform/features/timestamp
- https://help.getzep.com/graphiti/core-concepts/adding-episodes



#### Research Insights

**From Solutions Archive:**
- .claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md — the two-layer config split that makes lowering one limit a silent no-op is the same defect class: one reader saw the real value, another saw a fallback.
- MEMORY INDEX (project) — `Cadence counters must persist`: in-memory 'every N turns' gates are defeated by restarts. For a single long-lived eval process the polarity INVERTS — see edge cases.
- MEMORY INDEX (project) — `SHIPPED: agentic consolidation on arcrun`: the sleep pass is a bounded arcrun ReAct agent with a pipeline fallback. That fallback is where the cost lands here.

**Best Practices:**
- Drive consolidation directly. `consolidate_poll_once()` is public and exported in `capabilities.__all__`; awaiting it returns only after the whole pass completes, returns a bool telling you whether the gates opened, and propagates exceptions to the harness instead of the loop swallowing them into a WARNING. This is not a framework change — it is calling a public function, and it is the ONLY reliable quiescence signal that exists today.
- Set FIVE knobs, not two: `brain = "arcmemory"`, a non-empty `distill_provider`, `consolidate_event_threshold`, `consolidate_idle_seconds`, `consolidate_interval_seconds`, AND `[modules.memory.config.dynamics] consolidate_interval_minutes = 0.0`. `distill_provider` outranks all of them — unset, there is no consolidator at all and every other setting is theatre.
- Do NOT run both the background loop and manual calls. There is no lock anywhere in arcmemory (`grep -rn 'asyncio.Lock' packages/arcmemory/src` returns zero hits), so two passes can interleave on the same SQLite connection and the same `.consolidate-manifest.json`. Either leave the arcagent thresholds at production values and force passes yourself, or accept the loop — never both.
- Belt-and-braces quiescence: read `<workspace>/memory/.consolidate-last-run` before and after and assert it advanced, and assert `.consolidate-manifest.json` is absent afterwards.
- Attach a `logging` handler to `arcmemory.consolidate` at WARNING for the whole run — it is the ONLY channel on which `dedup_skipped` and the degrade warnings are visible, because the audit sink is null.
- Consider `consolidate_engine = "pipeline"` in `dynamics` for the eval, to avoid paying for a doomed 20k-token agentic attempt before every pipeline pass — and RECORD that choice, because it changes what is being measured.
- Extend the D-500 preflight to a live end-to-end assertion in a scratch workspace: `_runtime.state().brain` is not a `NullBrain`; `brain._embedder`, `brain._distiller` and `brain._model` are all non-None after startup; one forced `consolidate_poll_once()` returns True; `.consolidate-last-run` advanced; the returned `episode_summary` reports `window_events > 0`; and a `memory/daily-log/*.md` exists. `arc agent build --check` tests none of this.

**Edge Cases:**
- D-499's cadence is unreachable by config: `_CONSOLIDATE_POLL_INTERVAL = 300.0` is a module constant (modules/memory/capabilities.py:35, 261, 271). The TOML knobs only gate the predicate INSIDE `consolidate_poll_once`. Forty session boundaries at 300s each is 3.3 hours of wall-clock waiting per question.
- `hygiene_due` SHORT-CIRCUITS the interval gate: `run_hygiene` calls `self.run(now=now)` directly without consulting `due()`. In a fresh per-question workspace the FIRST `consolidate()` always does real work; only calls 2..N are gated by `consolidate_interval_minutes`.
- Counter persistence inverts for a long-lived eval process. The in-memory arcagent counters (`events_since_consolidate`, `last_activity`) are the RELIABLE half — they never reset mid-run. The PERSISTED half is the dangerous one: `.consolidate-last-run` survives and suppresses the next 59 minutes of passes. D-500's throwaway-workspace-per-question isolates this.
- `events_since_consolidate = 0` executes AFTER the await, so every capture landing during a multi-minute pass is zeroed and never counts toward the next threshold.
- Silent no-op paths that never raise: empty `distill_provider` (no consolidator, empty result, no log, no audit); un-lowered `consolidate_interval_minutes` (empty result, no log); an empty curated window still STAMPS `.consolidate-last-run`, blocking the next pass for the full interval; `_parse` returns `{}` on any non-JSON completion so zero facts are extracted with no error; unavailable embeddings make `merge_cues` a silent `[]`.
- Every arcmemory-internal audit event is discarded. `_runtime.configure` calls `select_brain(...)` WITHOUT `audit_sink=`, so `_audit` falls back to `NullSink` — `memory.consolidation_degraded`, `memory.dedup_skipped`, `memory.fact_updated` and `memory.entity_merged` all vanish in a live agent. This is a latent framework observability bug worth its own ticket.
- `merge_entities()` runs inside EVERY consolidation pass, not nightly (consolidate.py:201). It needs BOTH an embedder and a confirmer (the distiller); without either it emits `dedup_skipped` and returns `[]`. Only the deterministic alias merge, backlink repair and workspace dedup are hygiene-only (once per local day).
- A run crossing local midnight escalates to the heavier full-hygiene pass instead of a normal consolidation. Unquantified cost; a long full-S run will hit it.
- `.consolidate-last-run` is workspace-scoped while `Consolidator` instances are per-Scope — the first scope to run blocks the others for the interval. Irrelevant to D-500's one-workspace-one-question design; a landmine for anything that shares a workspace.
- Module configure failure is FAIL-OPEN: `configure_module_runtimes` catches and continues, so a `dynamics` value that fails pydantic validation degrades to `NullBrain` and the agent starts normally with memory entirely off.
- `docs/config-reference.md:59-63` documents `curate_keep_tools` / `curate_min_substantive_chars` / `curate_tool_requires_entity` / `curate_tool_keep_salience`, none of which still exist in `arcmemory/config.py`. Anyone tuning curation from the docs is silently ignored.

**Performance:**
- COST BOMB: `Consolidator.run()` uses `window = TimeWindow()` with `start=None, end=None`, so every pass re-reads the ENTIRE episodic stream (consolidate.py:185-187; `EpisodicStore.events` is an unbounded `SELECT … ORDER BY seq`). With per-session consolidation over 40 sessions this is O(n^2) in LLM cost. A `start = last_run()` bound would fix it.
- The agentic engine is bounded at `max_turns=16`, `max_tokens=20_000` (cumulative input+output), `timeout=180s`. Rendering the whole stream into one task string breaches 20k tokens by roughly session 3 — so it degrades and the FULL PIPELINE runs anyway. You pay one large wasted call before every pass.
- Pipeline calls per pass, C = chunks in window, D = distinct days, F = fact candidates: `extract_facts` 1xC, `mint_insights` 1xC, `extract_procedures` 1xC, `summarize_day` 1 per day (D ~= 1 given D-498), `confirm_entity_merges` 1 total, plus an embedding call per fact candidate — and `resolve_entity` disambiguation at up to 1 call PER FACT CANDIDATE, UNBOUNDED. There is no per-pass call-count or cost budget on the pipeline path.
- There is no lock and no serialization: the background loop is serial with itself, but a harness that also calls `consolidate_poll_once()` can interleave two passes against the same SQLite connection and the same manifest file.

**References:**
- packages/arcagent/src/arcagent/modules/memory/capabilities.py:35, 232, 261-271, 274-318
- packages/arcagent/src/arcagent/modules/memory/config.py:46-50, 67-68
- packages/arcagent/src/arcagent/modules/memory/_runtime.py:85-87, 127-136
- packages/arcmemory/src/arcmemory/brain.py:178-202, 269-287
- packages/arcmemory/src/arcmemory/consolidate.py:154-155, 171-179, 185-194, 201, 220-259, 272-296, 495-515, 636-679
- packages/arcmemory/src/arcmemory/config.py:102-104, 111-113, 158-162
- packages/arcmemory/src/arcmemory/provider.py:46-52, 57, 100-114
- packages/arcrun/src/arcrun/strategies/react.py:60-66, 435-441



#### Research Insights

**From Solutions Archive:**
- .claude/solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md — unbounded resource consumption and circuit-breaker patterns; the spend ceiling below is the same control applied to token cost.
- .claude/solutions/security-issues/2026-02-21-arcrun-phase4-hardening-review-learnings.md — cleanup patterns; the `finally`-scoped workspace teardown below comes straight from it.
- MEMORY INDEX (project) — `Producers-unwired pattern`: demand E2E-through-the-real-path tests and never trust a worker's self-report. The live preflight is the concrete form of that rule here.

**Best Practices:**
- Make the QUESTION the atomic unit of resume, not the chunk. Write the result row only after ingest -> query -> judge all complete, with a terminal `"status":"complete"` field. A question is done iff a line for its `question_id` exists with that status; anything else — missing, partial, or unparseable — is rebuilt from scratch.
- Mark workspace readiness with an `.ingest_complete` marker written LAST. On resume, a workspace lacking it is garbage: delete and rebuild, never resume mid-ingest. Chunk-level LLM calls are order-sensitive and not naturally idempotent.
- Append-only, never read-modify-write. Build the done-set once at startup by reading the JSONL into a dict, keep the handle open in append mode, and never seek. Tolerate a truncated final line on load — that is the normal SIGKILL signature.
- `f.write(); f.flush()` per line is cheap; `os.fsync()` only every N completed questions or at clean shutdown. Note the reference `evaluate_qa.py` does neither and can lose its last buffered lines.
- Pin the judge by DATED model string (`gpt-4o-2024-08-06`, never the `gpt-4o` alias), `temperature=0`, `max_tokens=10`, and record the string in every result row. The alias silently repoints and breaks comparability between runs months apart. There is NO `seed` parameter in the real script — do not rely on one.
- Instantiate a SEPARATE client for the judge, with its own key variable and its own module. Never share a client, config dict or `messages` list between the system under test and the judge.
- Log judge disagreement as a first-class metric: double-judge a fixed sample or the borderline cases, log the agreement rate, and keep old labels in the row when the judge prompt or model changes so old and new can be diffed rather than silently overwritten.
- Ship a `--dry-run` / `--estimate` mode that walks the dataset through the REAL chunker (not `len(text)/4`), sums estimated tokens, and multiplies by a versioned pricing table kept in config. At ~40k calls a 20-30% token-count error compounds into a meaningfully wrong ceiling.
- Enforce a hard spend ceiling as a circuit breaker that ABORTS, not warns, at ~110% of the dry-run estimate — and persist the running total so it survives resume.
- Gate `--full` behind a `--smoke N` (3-5 questions across types) that runs ingest -> query -> judge end to end.
- Log `{tokens_in, tokens_out, cost_usd, n_llm_calls, wall_seconds}` per question. A median-cost diff against the prior run catches an ingest-cost regression before it burns the full-S budget.
- Every result row carries: `git_sha` (with dirty flag), `harness_version`, `config_hash` (sha256 of the canonicalized RESOLVED config, since env overrides matter), `dataset_sha256`, `agent_model_id`, `judge_model_id`, `embedder_model_id`, `distiller_model_id`, `run_timestamp_utc`, `question_id`. Write a `run_manifest.json` too, but keep the block redundantly in every row so a single row is self-describing after rows are concatenated across runs.
- Teardown runs in a `finally`/context manager, and the harness refuses to start a phase if leftover workspaces from an aborted run exceed a threshold — 'throwaway' workspaces that are never thrown away are the standard way these harnesses fill a disk.

**Edge Cases:**
- `print_qa_metrics.py` hard-asserts the judge string is `gpt-4o-2024-08-06` and CRASHES on any other judge (open issue #47). gpt-4o and gpt-4o-mini as judges agree only ~85.7%, with mini stricter — accuracy is not comparable across judge choices.
- `temperature=0` does not make the judge deterministic. Recent work shows 1-2 of 7 borderline items still flip under forced greedy decoding. Do not build an acceptance gate on a single judge call for borderline cases.
- `_abs` questions must be EXCLUDED from retrieval scoring (the reference filters `if '_abs' not in x['question_id']`) but INCLUDED in QA accuracy under a refusal-checking prompt. Running the standard 'does the response contain the answer' grader on them misscores all 30.
- Retrieval metrics are `recall_any@k` and `recall_all@k` — the default print script reports `recall_all@5`/`recall_all@10` at session level and adds `@50` at turn level. 'Recall' without the any/all qualifier is ambiguous and not comparable.
- 'Treating workspace-exists as question-complete' is the classic resume bug: a crash mid-ingest leaves a plausible-looking directory missing chunks, and it silently produces a worse answer on resume.
- SQLite WAL/SHM siblings are not removed by deleting the `.db`. Checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`) or close cleanly before deleting, and delete the whole workspace directory — a stray `-wal` from a crashed run can exceed the main db file.
- The three published-baseline traps: mem0's LongMemEval numbers come from marketing pages with undisclosed reader model and unconfirmed official-judge use; the benchmark's own issue tracker is full of unvetted self-reported 89-99% results; and none are comparable without matching dataset variant, judge model, reader LLM, and whether the official scripts were used verbatim.

**Performance:**
- Measured type distribution of LongMemEval-S (500q): multi-session 133, temporal-reasoning 133, knowledge-update 78, single-session-user 70, single-session-assistant 56, single-session-preference 30. Proportional 50q stratification gives roughly 13/13/8/7/6/3.
- Statistical floor for D-500's ~50q phase: at n<10 a Wilson interval is not stable; at n=13 with 10/13 correct the 95% CI is ~[50%, 92%]; at n=30 with 24/30 it is ~[62%, 91%]. You need n>=30 per stratum before the interval is narrow enough (~+/-15-17pp) to separate systems differing by less than ~20 points. At 50 total across 6 strata, only the POOLED accuracy supports a confident claim — per-type numbers are directional and must be printed with their CIs. Reserve per-type claims for full S (n=56-133 per stratum, ~+/-7-12pp).
- Disk: ~150-300KB raw session JSON + ~1.2-3MB of vectors (200-500 chunks x 1536-dim float32) + 1.5-2x sqlite/WAL/index overhead = roughly 3-8MB per question workspace, so ~1.5-4GB for 500 — fine on a laptop IF WAL files are checkpointed on teardown.
- Keep per question (a few KB each, cheap for all 500): the raw judge prompt and response, the final agent answer, the full result row, and a manifest of ingested chunk ids + hashes. Delete on success: the sqlite db, WAL/SHM and raw embedding vectors — they regenerate from the manifest. Keep them only for failed or judge-disagreement questions behind a `--keep-workspace-on-failure` flag.
- Paper baselines for orientation (GPT-4o judge): GPT-4o Oracle 0.870 (0.924 with Chain-of-Note) vs LongMemEval-S full-history 0.606 (0.640 with CoN) — a ~30% relative drop. Zep on S: 71.2% with gpt-4o vs 60.2% full-context. Commercial products in the paper's own pilot: ChatGPT+GPT-4o 0.5773, Coze+GPT-4o 0.3299.

**References:**
- https://raw.githubusercontent.com/xiaowu0162/LongMemEval/main/src/evaluation/evaluate_qa.py
- https://raw.githubusercontent.com/xiaowu0162/LongMemEval/main/src/evaluation/print_qa_metrics.py
- https://raw.githubusercontent.com/xiaowu0162/LongMemEval/main/src/retrieval/eval_utils.py
- https://github.com/xiaowu0162/LongMemEval/issues/47 — print_qa_metrics crash on non-gpt-4o judges
- https://github.com/xiaowu0162/LongMemEval/issues/50 — relative-date / gold-answer inconsistency
- https://arxiv.org/html/2606.26185v1 — temperature control is necessary but not sufficient for judge reproducibility
- https://sqlite.org/tempfiles.html — WAL/SHM lifecycle
- https://blog.getzep.com/state-of-the-art-agent-memory/
- https://mem0.ai/research — marketing-sourced LongMemEval numbers, methodology undisclosed

#### API Design

_(no decisions in this category for this feature)_

#### Observability

_(no decisions in this category for this feature)_

#### Audit & Compliance

_(no decisions in this category for this feature)_

#### Performance

_(no decisions in this category for this feature)_

#### Extensibility

_(no decisions in this category for this feature)_

#### Deployment

_(no decisions in this category for this feature)_

#### UI/UX

_(no decisions in this category for this feature)_


#### Open Questions
- Does the operator have an OpenAI API key available for the GPT-4o judge? The benchmark's evaluate_qa.py requires it, and comparability to published numbers depends on using the same judge. To be verified before the Oracle run.
- Model-commentary events from D-496 sit in the store alongside real evidence and may compete at recall time. Whether this measurably moves scores is unknown until the Oracle run; if it does, the ingest agent's reply handling is the first thing to revisit.
- Event.ts is uniform across a haystack (D-498), so recency ranking contributes nothing. If temporal-reasoning scores come back poor, this is the first confound to rule out before concluding memory is weak.
- Whether the sampled-S stratification should weight question types evenly or match the full set's natural distribution. Deferred until Oracle results show which types are weakest.

#### Research Insights

**From Solutions Archive:**
- MEMORY INDEX (project) — `Embedder silent-degrade` and `arcmemory distiller unwired` both resolved as 'the seam was dead and nothing said so'. Every open question below that ends in 'we will find out at the Oracle run' should instead be an assertion in the preflight.

**Best Practices:**
- Answer the two cheapest open questions BEFORE the Oracle run rather than after: (a) measure the LongMemEval turn-length distribution to learn whether any single turn exceeds `max_event_chars` (decides whether D-495's 'never split mid-turn' is even achievable); (b) run every chunk through `sanitize` + `privacy_filter` locally and count how often gold evidence shrinks (decides whether any score is trustworthy at all).
- The D-496 commentary-noise question is partly self-answering: at chunk sizes >= `max_event_chars` the joined user+assistant `respond` event truncates back to the `user` event and is deduped away, so no commentary event is stored. Below that size, every evidence chunk is stored twice. Choose the chunk size deliberately and record which regime the run is in.

**Edge Cases:**
- NEW — does the answering prompt tell the model what 'today' is, and what date does it give? If the answerer believes it is today and every `Fact.date` is stamped today, `temporal-reasoning` is unanswerable regardless of retrieval quality. This may dominate every other temporal confound listed here.
- NEW — the D-498 recency confound is Oracle-specific in DIRECTION. Oracle haystacks are unsorted, so the first phase of the plan is the arm where ingest-order recency is actively misleading; on S it is accidentally helpful. Do not read the Oracle temporal number as a memory-quality signal.
- NEW — is `arcllm`'s SPEC-038 budget / circuit breaker capable of tripping mid-run and turning distillation into a silent no-op? Not traced. A long full-S run is exactly the workload that would trip it.
- NEW — does `_current_did` stay bound if the harness wraps `run()` in `asyncio.create_task`/`gather`? The binding lives in the child task, so a parent-task `consolidate_poll_once()` would raise `MemoryIsolationError`. Verify empirically or call `activate_runtime_bindings(agent)` immediately before.
- RESOLVED — 'does the operator have an OpenAI key for the GPT-4o judge'. The requirement is sharper than logged: it must be `gpt-4o-2024-08-06` specifically, because `print_qa_metrics.py` hard-asserts that exact string and any other judge both crashes the script and breaks comparability. Still open underneath it: is the judge called in-process through arcllm (so its prompts land in the trace store) or by shelling out to `evaluate_qa.py` (so they land wherever that script writes)? Different artifact surfaces, different redaction requirements.
- NEW — sequential or parallel across the 500 questions? D-500 does not say, and the answer changes the required config: parallel makes per-run `ARCSTORE_DATA_DIR` and unique agent names MANDATORY rather than advisory, because of the flocked WORM chain.
- NEW — where exactly do run dirs live? `evaluations/longmemeval/runs/<qid>/` is assumed throughout this research, and the gitignore patterns depend on it. Not stated in D-494 or D-500.
- NEW — which tier does the eval agent run at? `MemoryConfig.for_tier` changes `alpha` and the other write/decay dynamics, and federal 'writes slower'. Whatever is chosen, the LongMemEval number is TIER-SPECIFIC and must be reported as such. Not decided anywhere in D-492..D-500.
- NEW — disabling `workpad` and `policy` is a validity decision, not only a security one. Left on, the run measures arcmemory plus two other LLM summarizers writing into the system prompt; turned off, the setup deviates from 'a real production agent', which is D-496's whole premise. This needs an explicit call, recorded.

**Performance:**
- Two cheap diagnostics that split 'memory is weak' from 'the harness confounded it', both runnable without a framework change: (1) use the recall-vs-QA split D-500 already records — temporal recall normal but QA low means the WRITE path (collapsed daily-log, facts stamped today) is the confound, not ranking; (2) `await brain.rebuild_index()` NULLs `chunks.mtime`, collapsing the recency list to a constant ordering, so re-running only the query step with and without it ablates the recency channel on an already-ingested workspace at zero re-ingest cost.
- The stronger 2x2 ablation, harness-side only, on ~20 temporal questions: prefix on/off x dataset-order/shuffled, plus an oracle-ts arm that writes the true `haystack_date` into `episodic.ts` AND `chunks.mtime` with two SQL statements. That last arm is the ceiling a real timestamp would buy and is the number that decides whether to revisit D-498. Run the same arms on `single-session-user` as a control — if the deltas are the same size there, it is generic retrieval noise.

**References:**
- packages/arcmemory/src/arcmemory/index/rebuild.py:144 — `rebuild_index()` NULLs mtime, which is what makes the free recency ablation possible
- https://arxiv.org/abs/2410.10813 — paper section 5.4 on time-agnostic memory designs
- https://github.com/xiaowu0162/LongMemEval/issues/50

#### Related Solutions
_(none)_


---

---
