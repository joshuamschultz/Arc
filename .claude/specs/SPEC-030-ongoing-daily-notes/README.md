# SPEC-030 — Ongoing Daily Notes (Decoupled From Compaction)

**Status**: PENDING | **Created**: 2026-07-02 | **Type**: single-module (arcagent `modules/memory`)
**Priority framework**: Simplicity → Modularity → Security → Scalability (principled-coder)

## One-line

Make daily notes an ongoing, crash-safe memory stream with periodic background
consolidation — triggered by turns / session-end / new-day, **never by context compaction**.

## Why

SPEC-029 made `transform_context` identity and removed the `agent:pre_compaction` event.
Its only remaining subscriber — `memory_pre_compaction`, which nudged the daily-notes file at
compaction time — is now **orphaned** (no emitter). More importantly, coupling note-taking to
compaction is a documented anti-pattern: it is exactly what MemGPT did and what Letta's
"sleep-time compute" was designed to undo. Memory quality should not be hostage to token
pressure, and token pressure should not force a premature reflection.

## Research consensus (in-conversation, 2 web agents)

Two-tier model, unanimous across Anthropic (memory tool + long-running-agent harness), Letta
sleep-time agents, Generative Agents (Park et al.), Reflexion, Mem0/A-MEM/MemoryBank, LangMem,
CrewAI, AutoGen:

- **Capture is continuous and cheap** — raw append per notable turn, **no LLM** on the hot path
  ("ASSUME INTERRUPTION": sessions don't always end cleanly).
- **Consolidation is periodic and background** — gated on accumulated signal / natural
  boundaries (session end, new day), executed off the interactive critical path for latency +
  cost isolation. Never on context-window pressure.
- **Anti-patterns:** (a) LLM-mediated write every turn (LangMem hot-path critique); (b) triggering
  memory work on compaction/memory-pressure (MemGPT → fixed by sleep-time compute).

Sources: arXiv 2304.03442 (Generative Agents — reflection at importance-sum threshold, ~2–3×/day),
2303.11366 (Reflexion — per-episode), 2504.13171 + Letta docs (sleep-time, N=5 steps, off critical
path), 2504.19413 (Mem0 — per-turn extract+update), Anthropic "Effective harnesses for long-running
agents", LangMem hot-path vs background.

## What arc already has (leverage, don't rebuild)

- `modules/memory/capabilities.py` (active SPEC-021 unified path; `markdown_memory.MarkdownMemoryModule`
  is legacy/uninstantiated).
- `@background_task entity_extraction_loop` (interval 1.0s) drains a single-slot `pending_messages`
  mailbox and runs eval-model entity extraction — the sleep-time layer already exists.
- `agent:post_respond` hook stages messages + `_ensure_daily_notes()` → `<workspace>/notes/<YYYY-MM-DD>.md`.
- `agent:shutdown` hook drains background tasks. `NoteManager` (append-only, `get_recent_notes`).
- **Dead:** `memory_pre_compaction` (`capabilities.py:263`) + the legacy `bus.subscribe("agent:pre_compaction")`.

## Scope

Three tiers, all inside `modules/memory`, all independent of compaction:

| Tier | Trigger | LLM? | Maps to |
|------|---------|------|---------|
| 1 Capture | every notable turn (post_respond) | No (raw append) | new — crash-safety layer |
| 2 Enrich | background loop (existing interval) | Yes, off-path | existing `entity_extraction_loop` |
| 2b Session consolidate | `agent:shutdown` / session close | Yes, one pass | new |
| 3 Day rollup | lazy on new-day file creation | Yes, background | new |

## Definition of Done

- [ ] `memory_pre_compaction` hook + `agent:pre_compaction` subscriber removed; nothing ties notes to compaction.
- [ ] Per-turn raw append lands immediately (no LLM), survives a crash before the background loop runs.
- [ ] Session-end consolidation runs on shutdown (one-shot runs get consolidated).
- [ ] New-day rollup fires lazily for both a daemon crossing midnight and a next-day one-shot run.
- [ ] All consolidation LLM calls go through the arcllm eval model and are sanitized before write (ASI-06).
- [ ] No arcrun/arcllm changes; memory-module only.
- [ ] `ruff` + `mypy --strict` clean; arcagent suite green; new tests for each tier.

## Learnings

_(populated during /implement and /review)_
