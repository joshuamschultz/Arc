# coding-agent-working-dir — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-489–D-491 (3 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Coding-agent working directory — Build Decisions (2026-07-26)

Phase: build | Status: complete | Total decisions: 3 (D-489 to D-491) | See: ADR-029, blueprints/coding, SPEC-058

**Context.** A coding agent must work in the project directory you open it in (like Claude Code / OpenCode) while its own brain — memory, sessions, context.md, identity — stays in one stable home. Those two were the same `workspace` in code; conflating them forced a bad choice (pin to workspace → can't work in your project; point at cwd → the agent's memory scatters into every repo you open).

| # | Decision | Choice | Priority | Rationale |
|---|----------|--------|----------|-----------|

---

### Deepening Summary

**Deepened on:** 2026-07-30
**Sections enhanced:** 7
**Solutions referenced:** 4
**Skills matched:** coding-workflow:plan-deepener, coding-workflow:python-patterns, coding-workflow:build-decisions, architecture-adr-generator, testing-coverage-gap-finder, academic-paper-breakdown

#### Key Findings
- BLOCKER — arcmemory's own `sanitize()` injection filter deletes the match AND everything to end of line (`_INJECTION_RE`, security.py:33-43). Two alternates are ordinary English: `you are now ...` and `forget everything/all previous ...`. Verified destructive on realistic benchmark sentences. This silently vaporizes evidence turns and corrupts the measurement itself, invisibly.
- BLOCKER — D-499's per-session cadence is unreachable by config. `_CONSOLIDATE_POLL_INTERVAL = 300.0` is a module constant, not a config key (modules/memory/capabilities.py:35). The both-limits claim is confirmed correct and still insufficient. Fix without a framework change: the harness awaits the public `consolidate_poll_once()` directly — the same function the loop calls — which is also the ONLY reliable quiescence signal that exists today.
- BLOCKER — the default recall envelope admits exactly ONE 2000-char chunk. `top_k=5` but `budget=1024` tokens; a 2000-char event is ~525 tokens and `enforce_budget` (security.py:261-280) counts the boundary-marked block, so 525+525 > 1024. Unchanged, the run benchmarks `enforce_budget`, not memory.
- BLOCKER — 500 throwaway agents sharing an `agent.name` collide on an exclusive flock over a SHARED WORM audit chain (`~/.arc/…/worm/audit-chain-<slug>.jsonl`, core/agent.py:183-209 + arctrust/audit.py:238-250). The second concurrent agent raises RuntimeError at startup. Needs a unique name per question and a workspace-relative `security.policy_audit_log`.
- D-498's rationale is refuted on its own terms. The LongMemEval reference implementation keeps `haystack_dates` as a PARALLEL METADATA ARRAY and ships `temp_query_search_pruning.py` to filter candidates by an inferred time range; the authors measure +7-11% temporal recall from it. Zep (`reference_time=`) and mem0 (`timestamp=`) both take a first-class timestamp parameter. Inline-in-text is not what they do.
- The `Event.ts` consequence in D-498 is stated backwards. `datetime.now(UTC)` has microsecond resolution, so sessions are NOT uniform — they are perfectly ordered by INGEST ORDER. Recency is an unweighted 4th RRF list worth up to 25% of the max achievable score. On LongMemEval-S (sessions timestamp-sorted) this is accidentally correct; on ORACLE (explicitly unsorted) it hands 25% of max score to an arbitrary 5 sessions — and D-500 runs Oracle FIRST.
- `capture_respond` joins EVERY message in the payload — the user turn and the assistant turn (capabilities.py:220-221 + agent_dispatch.py:333-340). Sub-2000-char chunks are therefore stored TWICE; >=2000-char chunks truncate back to the `user` event byte-for-byte and the Deduper drops them, so the reply is never stored. D-496's open question about commentary noise is both worse and, at the cap, self-cancelling.
- `Consolidator.run()` uses an unbounded `TimeWindow()` (consolidate.py:185) — every pass re-distills the ENTIRE episodic stream from event 0. Per-session consolidation over 40 sessions is O(n^2) in LLM cost, and it guarantees the agentic engine breaches `max_tokens=20_000` after ~3 sessions, degrades, and pays for the pipeline anyway.
- Every arcmemory-internal audit event is discarded in a live agent. `_runtime.configure` calls `select_brain(...)` without `audit_sink=` (modules/memory/_runtime.py:127-136 vs brain/select.py:50), so `_audit` falls back to `NullSink`. `memory.dedup_skipped`, `memory.consolidation_degraded`, `memory.fact_updated` all vanish. Watch the `arcmemory.consolidate` Python logger instead.
- Ground truth for D-500's stratification, measured from the dataset: multi-session 133, temporal-reasoning 133, knowledge-update 78, single-session-user 70, single-session-assistant 56, single-session-preference 30 (with 30 `_abs` cross-tagged). Proportional 50q sampling puts single-session-preference at n=3. At n<10 a Wilson interval is not even stable; at n=13 and 77% observed the 95% CI is roughly [50%, 92%].
- The official judge pins the DATED string `gpt-4o-2024-08-06`, `temperature=0`, `max_tokens=10`, and `print_qa_metrics.py` hard-asserts that exact string (open issue #47). There is NO `seed` parameter in the real script despite secondary sources claiming one. The reference judge is also a single-threaded loop with no per-line flush.
- BLOCKER — D-492 is not enforced by anything today. `git check-ignore` confirms that `evaluations/**/data/*.json`, run workspaces, `traces/*.jsonl`, `.audit/*.worm` and results JSONL are ALL currently trackable. There is one `.gitignore` in the repo and it does not mention `evaluations/`. The patterns must land before the first run, and the preflight should hard-fail on `git check-ignore -q <run_dir>`.
- BLOCKER — `[modules.telemetry] store_raw_bodies = true` is the arcllm DEFAULT, encryption off. That writes full prompt and response bodies — every haystack chunk verbatim — as plaintext JSONL into `<agent_root>/traces/`, which for an `evaluations/`-rooted agent is inside the repo tree and currently not ignored. Several GB per full run.
- BLOCKER — `ArcAgent(cfg)` without `config_path` resolves `./workspace` against the PROCESS CWD, not the config. `agent.py:105` sets the attribute but line 109 branches on the PARAMETER, so the natural programmatic call puts the workspace, `traces/`, `.audit/` and the capability scan root at the arc repo root. Always pass `config_path=<abs>/arcagent.toml` and assert `agent._workspace` is under the run dir.
- D-493's premise holds for the WRITE path only. `sanitize()`/`privacy_filter()` clean the copy being STORED; the raw chunk still reaches the model as a user-role message with no boundary marking (agent_dispatch.py:112,132). The trust boundary is a memory-integrity boundary, not a model-input boundary. Recall read-back IS correctly boundary-marked and defanged — but `context.md` (workpad) and `policy.md` land in the SYSTEM PROMPT filtered only by `utils/sanitizer.py`, which has no injection-drop at all.
- At personal tier `bash` is an UNFENCED host shell, and the capability ledger tags `subprocess` as `untrusted_input` ONLY — justified by a comment asserting `--network=none`, which is false at personal tier. So `read` + `bash curl` never completes the trifecta and HumanGate never fires. A default-scaffolded agent ships ~35 LLM-callable tools with `allow = []` (allow-all) and 40 agentic turns per chunk. The ingest agent needs ZERO tools.
- The benchmark's scoring is not one number. Task-averaged accuracy is a MACRO mean of the six per-type accuracies; overall accuracy is the micro mean; abstention accuracy is reported separately. `single-session-preference`'s `answer` field is a RUBRIC, not a literal string, and gets its own judge prompt. `_abs` questions use a refusal-checking prompt and are EXCLUDED from retrieval scoring entirely.

#### New Risks Discovered
- Measurement-corrupting input filter: `_INJECTION_RE` and `privacy_filter`'s `secret|password|token[:=]` pattern can delete gold evidence before it is ever stored, producing a real-looking low score for a crippled input path.
- Oracle-phase recency artifact: Oracle haystacks are unsorted, so the first phase of D-500 is the arm where the recency channel is actively misleading rather than merely uninformative.
- Cost blowout well past D-496's '~one LLM call per chunk': unbounded consolidation window (O(n^2)), a doomed 20k-token agentic attempt before every pipeline pass, full session history re-sent every turn if all chunks share one session key, plus unbounded `resolve_entity` disambiguation calls with no per-pass budget.
- Silent truncation of an oversized single turn is undetectable through the public path — `_capture` discards `capture()`'s return value (capabilities.py:231) — so D-495's 'never split mid-turn' has no enforcement and no alarm.
- Write-path temporal damage D-498 does not mention: all 40 sessions bucket into ONE `daily-log/<today>.md`, the day-summary prompt is handed ingest-clock HH:MM, and every `Fact.date` is stamped today — which degrades `knowledge-update`'s `| was:` contradiction trail, not just `temporal-reasoning`.
- Default-on scaffold modules (workpad context.md rewrite, policy eval, scheduler, skills sweep, messaging/tasks dialling NATS, shared arcstore) add unbudgeted LLM calls, latency, shared-state writes, and startup failures at 500x.
- Dataset version drift and known gold-label errors: the Sept-2025 'cleaned' revision is not numerically comparable to the original, and issues #37-41/#50 document relative-date and gold-answer mistakes still open.
- Comparability trap: published mem0 LongMemEval numbers come from marketing pages with undisclosed reader model and unconfirmed use of the official judge — they are not a valid target to beat.
- Self-modification tools (`create_tool`, `create_skill`, `update_tool`, `reload`) carry ZERO trifecta legs, and `~/.arc/capabilities` is a GLOBAL scan root shared by every agent — so a tool planted by one question is loaded by every subsequent question and persists on the host.
- 500 agents share `~/.arc/store` and one flocked WORM chain slugged from the agent NAME, so any parallelism raises `RuntimeError` and sequential runs still cross-contaminate the operational spool.
- The harness will be reused for email and Slack adapters (D-497), where the corpus genuinely IS adversarial. The tool-surface and boundary mitigations belong at the seam now, not retrofitted then — that retrofit is exactly the federal-preservation rule this project exists to avoid.
- No dataset integrity check is decided anywhere in D-492..D-500. A third-party download feeding a memory store is LLM04 territory; a SHA-256 in the run manifest, verified at preflight, closes it and makes runs reproducible at the same time.

---
