# NOTE — Spool write path: pooling + queues (follow-up from SPEC-028 /review)

**Raised:** 2026-06-01, from the SPEC-028 performance review.
**Status:** Investigation needed — NOT scoped into SPEC-028. Candidate for its own spec.
**Owner concern:** arcstore (write path) + arcrun/arcllm (callers). Cross-cutting.

## The problem

Every operational record (`llm_call`, `run_event`, and now `tool_event`) is written
to the spool synchronously, on the calling coroutine, with no offload and no
batching. SPEC-028 made this sharper because **tool events are the volume driver**:
~10–50 `llm_call`s per run vs **10–1000+ tool events** per run.

Write path today:
- `arcrun/events.py` `EventBus.emit()` → `_record_run_event()` → `arcstore.spool.record()`
  runs **inline on the async executor coroutine** (no `await`, no `to_thread`).
- `arcstore/spool.py` `record()` does, **per record**: `mkdir` (first call) +
  `os.open` + `os.fchmod` + `os.write` + `os.close` — 4 blocking syscalls each.
- Same pattern in `arcllm/modules/telemetry.py` `_record_spool()` and in
  `arcagent/orchestration/spawn.py` `_spool_spawn_event()`.

On the **read** side, the arcui mirror compounds it: `arcstore/backends/sqlite.py`
opens a **fresh SQLite connection per query** (3 PRAGMAs each), the mirror tables
have **no indexes**, and `arcui/observe.py` windowed reads pull up to `limit=100_000`
rows and filter by `ts` in Python.

## Why it matters

This conflicts directly with the codebase's Scalability pillar ("1000s of agents
concurrently, async-first, no hot-path blocking, connection pooling on everything
external"). With many agents sharing one event loop, every tool boundary stalls
the loop for the duration of the syscalls; a heavy code-gen / tool loop serializes
all agents behind disk latency. The `limit=100_000` read cap also **silently
truncates** a window once the mirror exceeds 100k rows.

It is **fine at the current single-operator target** — one loop, low contention,
thousands of rows. This is a ceiling to address before multi-agent load, not a
bug today. (Recorded as a "known ceiling" in SDD §12.)

## Directions to investigate (write path)

1. **Single-writer queue (fan-in).** Producers `put` records onto a bounded
   `asyncio.Queue`; one drain task owns the daily fd and does batched `os.write`s.
   `O_APPEND` writes are already atomic, so a single writer removes the
   open/fchmod/close-per-record overhead entirely and gets the I/O off the hot
   coroutine. **Bounded** queue = backpressure: decide drop-oldest vs block vs
   shed (telemetry should shed, never block the audited call — fail-open still holds).
2. **Held-open fd per process.** Cheaper interim step: keep today's spool fd open,
   tighten perms once, `os.write` without reopen. Cuts 4 syscalls/record → 1.
   No queue, smaller change. Doesn't get I/O off the loop, but removes the syscall
   churn.
3. **Offload to a thread.** Wrap `record()` in `asyncio.to_thread` / run_in_executor
   when a loop is running (mirror the existing `_schedule_async_observer` pattern in
   `events.py`). Simplest "get it off the loop," but a thread-per-write is worse than
   a single drain task under high volume — prefer (1).

## Directions to investigate (read path)

4. **Connection pooling** for the arcui mirror — reuse connections instead of
   open-per-query (the comment in `sqlite.py` notes shared-nothing per *instance*;
   a per-instance pool is compatible).
5. **Push `WHERE ts >= cutoff` into SQL** + add indexes on `(ts)` and `(request_id)`;
   remove/raise the `limit=100_000` truncation so windows can't drop rows.
6. **`asyncio.gather` the 3 timeline reads** in `observe.py::timeline` (independent
   table queries currently awaited sequentially).

## Open questions / measure first

- What's the real per-record `record()` latency on the target disk, and the tool-event
  rate of a heavy run? (Decides whether (2) alone suffices or (1) is required.)
- Queue overflow policy under sustained load — shed which records? (Never errors,
  `run_event`, or `spawn_event` — same rule as sampling, NFR-5.)
- Does the WORM (`arctrust`) need the same treatment, or does its durability contract
  (per-record fsync) make a queue inappropriate there? (Spool ≠ WORM — different
  durability guarantees.)
- Sampling (`sample_rate`) already thins `tool_event` volume — is queue+pooling
  needed at the same time, or does sampling buy enough headroom for v1?

## Related

- SDD §12 "Known ceilings" (this spec).
- SPEC-026 (arcstore spool + ingest design) — the write/read primitives live there;
  any change here amends that data plane.
- Perf review also flagged: swap `secrets.randbelow` → a plain PRNG in
  `EventBus._should_sample` (CSPRNG is needless on the sampling path; LOW).
