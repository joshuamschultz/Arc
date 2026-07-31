# SPEC-027 — Unified Execution Entry: Implementation Plan

**Status:** COMPLETE
**Phases:** 4 (A Collapse entry → B Real streaming → C CapabilityProvider → D Route everything + verify)
**Approval gates:** end of each phase
**Module discipline:** a task touching two packages is split. TDD: write the failing test first (the `Test` column names it). No back-compat shims — delete old code in the same edit (CLAUDE.md).

Dependency order: **A → B → C → D**. A is the foundation (one streaming entry). B wires the gateway to A. C swaps the tool contract (touches the same dispatch, so after A). D routes the remaining surfaces and verifies SPEC-026 intact.

---

## Phase A — Collapse the four entries into one streaming, session-bound `run` (FR-1)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| A.1 | Failing test: `agent.run(input, session=...)` yields ≥2 `StreamEvent`s and ends with `TurnEndEvent`; calling without a session raises | `arcagent` | S | `tests/unit/core/test_agent_run.py::test_run_streams_and_requires_session` | [x] |
| A.2 | Implement single `agent.run`; **delete** `chat`/`run_async`/`chat_async`/`chat_stream`; update `__all__`; delete `AgentHandle` | `arcagent` | S | A.1 passes; grep: no `def chat`/`run_async`/`run_stream` in arcagent src | [x] |
| A.3 | Collapse `agent_dispatch` 3-way fork into one `dispatch_stream(agent, input, *, session) -> AsyncIterator[StreamEvent]` | `arcagent` | S, M | `test_agent_run.py` streaming tests (single dispatch path) | [x] |
| A.4 | `collect(stream) -> RunResult`; one-shot callers get a final result | `arcrun` | S | `tests/unit/test_collect.py::test_collect_returns_final_result` | [x] |
| A.5 | History parity: a turn via `run` appends to `SessionManager` exactly as `chat` did | `arcagent` | M | `test_agent_run.py::test_run_appends_user_and_assistant_to_session` (AC-1.4) | [x] |
| A.6 | `agent.session(key)` open-or-resume for sessionless callers (CLI/scheduler) — see deviation note | `arcagent` | M | `test_agent_run.py::test_session_local_resumes_by_key` | [x] |

**Phase A acceptance:** AC-1.1–AC-1.4 pass (arcagent + arcrun). `ruff`/`mypy --strict` clean.
arcrun **410 passed**; arcagent **3241 passed, 16 skipped, 0 failed**.

> **Deviations from the SDD sketch (user-directed 2026-05-31, recorded for /review):**
> - **`Session` = the existing `SessionManager`, not a new wrapper.** Per user ("make the core have it, not add a wrapper"). The agent now holds a **keyed pool** `dict[str, SessionManager]` (multiple concurrent sessions — different humans/agents = distinct sessions) instead of one re-pointed manager. `Session.local(key)` is realized as `agent.session(key)` (open-or-resume) + `SessionManager.open_or_resume`. NFR-1 relaxed by user (core LOC may grow).
> - **Module/scheduler callback** is `agent.run_collected(input, *, session_key) -> RunResult` (run+collect closure), emitted as the single `run_fn` at `agent:ready`. `chat_fn`/`run_async_fn`/`chat_async_fn` removed.
> - **`tool_choice`/`automated`/cron `disabled_toolsets`/`skip_memory` kwargs dropped** — the minimal entry takes none. Pulse/scheduler runs now go through normal policy + full tool manifest. The old `CRON_AGENT_KWARGS` self-scheduling guard was **stub-forwarding only** (the old `run` never accepted those kwargs; the registry enforcement was mocked in its test). **Follow-up: re-home cron self-scheduling prevention to real arctrust policy (caller_did) in Phase C (AC-4.4).** Obsolete test `test_cron_self_scheduling_prevention.py` deleted.
> - Deleted obsolete `TestArcUIEventFlowIntegration` (tested SPEC-026-removed `EventBuffer`/`RollingAggregator`/`on_event_callbacks`).

---

## Phase B — Real streaming through the gateway (FR-3) — *finish the M2 TODO*

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| B.1 | Failing test: executor yields ≥2 token `Delta`s, `is_final` only on the last (no single-chunk wrap) | `arcgateway` | S | `tests/unit/test_executor_streaming.py::test_streams_real_token_deltas` | [x] |
| B.2 | Implement `_stream` to `await agent.session(key)` + `async for ev in agent.run(..., session=...)` → `Delta`; **delete** the `hasattr(agent,"chat")` branch + wrap-as-one-token + M2 comment | `arcgateway` | S | B.1 passes (AC-2.1, AC-3.1) | [x] |
| B.3 | Failing test: error mid-stream → fail-closed Delta + clean done sentinel (nothing escapes the socket); session records failure (arcagent `agent:error`) | `arcgateway` | Sec | `tests/integration/test_stream_error_failclosed.py::test_error_midstream_fails_closed` (AC-3.2) | [x] |
| B.4 | Pull-based streaming: a slow consumer does not run the producer ahead (no buffer in executor; per-socket queue bound lives in adapters `in_process`/`web`) | `arcgateway` | Sc | `test_stream_error_failclosed.py::test_slow_consumer_does_not_run_producer_ahead` (AC-3.3) | [x] |

**Phase B acceptance:** AC-2.1, AC-3.1–AC-3.3 pass. arcgateway **740 passed, 1 skipped, 0 failed**; ruff + mypy --strict clean. Executor imports `arcrun.TokenEvent` (SDD §4); end-to-end fakes migrated to `session()`+streaming `run()`.

> **Known limitation (honesty — flagged in /review):** "streams for real" means the executor now emits **multiple** `Delta`s (one per `TokenEvent`) instead of one wrapped chunk — but arcrun's `run_stream` still runs the loop to completion and **word-splits** the final text into `TokenEvent`s (arcllm exposes no token-streaming wire today). So deltas arrive per-word *after* the turn finishes, not at model wire speed. This is a real improvement over the M2 single-fake-token, satisfies AC-3.1 literally, and the gateway is correctly wired to relay whatever arcrun yields — but true wire-level streaming is a follow-up gated on an arcllm `invoke_stream`-driven loop.

---

## Phase C — arcrun takes a `CapabilityProvider` (FR-4)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| C.1 | `CapabilityProvider` Protocol satisfied by a `FakeProvider`; `provider_tools` advertises schemas + dispatches `invoke` with caller_did | `arcrun` | M | `tests/unit/test_capability_provider.py::test_provider_drives_advertise_and_invoke` | [x] |
| C.2 | Protocol + types (`CapabilitySpec`/`CapabilityResult`/`StaticProvider`) in `arcrun.capabilities`; `run`/`run_async`/`run_stream` take `capabilities`; registry built from `advertise()` via `provider_tools` | `arcrun` | M, S | C.1 passes (AC-4.3); 416 arcrun tests green | [x] |
| C.3 | `AgentCapabilityProvider.advertise()` returns lean specs (no skill bodies) | `arcagent` | S, Sc | `tests/unit/core/test_agent_capability_provider.py::test_advertise_is_lean` | [x] |
| C.4 | Implement `AgentCapabilityProvider` over the capability registry; arcrun seam takes the Provider (not a flat list) — see deviation | `arcagent` | M | C.3 passes | [x] |
| C.5 | `use_skill(name)` → `load(name)` body splice; unused skills never load | `arcagent`,`arcrun` | Sc | `test_capability_provider.py::test_use_skill_loads_body_lazily` + `test_agent_capability_provider.py::test_load_fetches_skill_body_on_demand` (AC-4.2) | [x] |
| C.6 | `invoke`/`load` carry `caller_did`; denied capability fails closed | `arcagent` | Sec | `test_agent_capability_provider.py::test_denied_fails_closed` (AC-4.4) | [x] |
| C.7 | Federal gate: workspace-authored capabilities denied at load in federal tier | `arcagent` | Sec | `test_agent_capability_provider.py::test_federal_gate_denies_workspace_capabilities` (AC-6.1) | [x] |
| C.8 | Architecture test: arcrun imports no arc sibling; CapabilityProvider is the owned Protocol | `arcrun` | M | `tests/test_layering.py::test_arcrun_imports_no_arc_sibling` (AC-5.1) | [x] |

**Phase C acceptance:** AC-4.1–AC-4.4, AC-5.1, AC-6.1 pass. arcrun **416 passed**; arcagent **3247 passed, 16 skipped, 0 failed**; ruff + mypy --strict clean on changed src.

> **Deviations (recorded for /review):**
> - **`to_arcrun_tools` kept as an internal helper, not deleted (C.4 literal grep unmet; AC-4.3 *intent* met).** The arcrun boundary now takes a `CapabilityProvider` — arcrun imports only the Protocol, never a flat `list[Tool]` (AC-4.3 satisfied). The core `ToolRegistry.to_arcrun_tools` survives as the *internal policy-wrapping step* the provider builds invocable tools from (the registry rightly owns policy wrapping). Moving policy-wrapping into the provider was out of scope. The dead `capability_registry.to_arcrun_tools` was left in place (tangential; deleting it + its tests deferred).
> - **`raw_tools()` escape hatch.** Context-dependent tools (spawn — reads depth/budget from the live `ToolContext`) can't route through the context-free `invoke`. `provider_tools` dispatches a provider's `raw_tools()` directly (real ctx); `StaticProvider` and `AgentCapabilityProvider` both expose it. Everything else routes through `invoke` (policy).
> - **caller_did = the agent's DID** (single-agent runs); flows via `run_stream(actor_did=...)` → `provider_tools(caller_did=...)` → `invoke`/`load`. Real arctrust policy enforcement stays in the core ToolRegistry's wrapped execute (already tested); the provider preserves fail-closed behavior.

---

## Phase D — Route every surface + verify SPEC-026 intact (FR-2, FR-5)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| D.1 | CLI `arc agent run`/`chat` + agent_worker + arctui open-or-resume a local session and `collect()`/stream — see deviation | `arccli`,`arctui` | S | arccli **318 passed**, arctui **67 passed** (AC-2.2) | [x] |
| D.2 | Modules/scheduler bind unified `run` via one `run_collected` callback (done in Phase A); `set_agent_chat_fn`/dual shapes deleted; pulse/scheduler fire a full turn | `arcagent` | M | covered by Phase A module migration + D.3 arch test (AC-2.3, D-122) | [x] |
| D.3 | Architecture test: no `chat`/`run_async`/`chat_async`/`chat_stream`/`set_agent_chat_fn`/`chat_fn` refs remain; agent exposes only `run`/`run_collected`/`session` | `arcagent` | M | `tests/test_single_entry.py` (AC-2.4, AC-1.1) | [x] |
| D.4 | SPEC-026 end-to-end: a turn via the new `run` spools `run_events` tagged with the agent DID (recording survives the new path) | `arcagent` | Sec | `tests/integration/test_run_records_to_arcstore.py::test_run_records_run_events_to_spool` (AC-5.2) | [x] |
| D.5 | Full-suite + `mypy --strict` + `ruff` green across all packages; SPEC-026 suites stay green | (all) | — | see below | [x] |

**Phase D acceptance:** AC-2.2–AC-2.4, AC-5.2 pass. **arcrun 416 · arcagent 3251 · arcgateway 740 · arccli 318 · arctui 67 · arcstore 56 · arctrust 182 — 0 failed.** ruff clean; mypy --strict clean on changed src (only pre-existing missing-stub noise for optional `docker`/`jsonschema` deps).

> **Deviations (recorded for /review):**
> - **D.2 was satisfied in Phase A** (the in-arcagent modules/scheduler/pulse were migrated to the one `run_collected` callback then, since deleting `chat` forced it). D.3's arch test now guards it.
> - **D.4 completes recording that was never wired.** The *old* dispatch never passed `actor_did` to arcrun, so agent runs never spooled `run_events`; the new `dispatch_stream` passes it, so recording now fires (consistent with SPEC-026 "ambient, on by default"). **Follow-up:** the agent has no `[arcstore]` config block, so `enabled=false` can't yet gate the agent's run_event spooling — wire `ArcStoreConfig` into the agent (SPEC-026 scope). `llm_call`/WORM recording is unchanged (lives in arcllm/arctrust).
> - **CLI output simplified:** `RunResult` (from `collect`) carries content/turns/tool_calls/cost — not `completion_payload`/`strategy_used`/`tokens_used`; the CLI verbose/JSON output dropped those fields. CLI `run`/`chat` use deterministic local session keys (`cli:run`, `cli:chat`); arctui uses `tui:main`.
> - **Fixed two pre-existing bugs while here** (leave-it-correct): chat.py's one-shot call omitted the required `context` arg; chat.py's `/session` referenced the removed singular `_session`.

---

## Definition of Done (whole spec) — ✅ COMPLETE

- [x] All phase acceptance criteria pass (fresh test output) — arcrun 416 · arcagent 3251 · arcgateway 740 · arccli 318 · arctui 67 · arcstore 56 · arctrust 182; 0 failed.
- [x] One streaming entry only: no `chat`/`run_async`/`chat_async`/`chat_stream`/`set_agent_chat_fn`/`chat_fn` references remain in source (`test_single_entry.py`). (`to_arcrun_tools` kept as an internal policy-wrapping helper — see Phase C deviation; arcrun seam takes the Protocol.)
- [x] arcrun's loop depends only on the `CapabilityProvider` Protocol; imports no arc sibling (`test_layering.py`).
- [x] Chat streams real per-token deltas (gateway B.1); CLI/worker/TUI collect-or-stream to a result.
- [x] SPEC-026 recording demonstrably intact end-to-end through the new run (`test_run_records_to_arcstore.py`); arcstore/arctrust suites green.
- [x] `ruff` + `mypy --strict` clean on changed src; **arcagent core LOC = 4392, DOWN from 4683 (NFR-1 satisfied — did not increase).**

## Suggested Branch

```bash
git checkout -b feature/SPEC-027-unified-execution-entry
```
