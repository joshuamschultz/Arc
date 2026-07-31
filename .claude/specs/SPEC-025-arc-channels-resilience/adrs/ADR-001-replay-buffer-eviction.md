# ADR-001 — Replay-buffer cleanup policy: TTL-deferred eviction

**Status:** Accepted (2026-05-06)
**Spec:** SPEC-025
**Pillar trace:** Simplicity, Scalability, Modularity

## Context

The SPEC-025 Track A replay buffer keeps the last 50 outbound frames per
`chat_id` so a reconnecting browser can ask `?since_seq=N` and the adapter
fills in what it missed. Two reviewers (security L2, architecture Major #1)
flagged that `_outbound_seq` and `_replay_buffers` were never reclaimed
when the last socket for a `chat_id` unregistered — the adapter accumulated
one ring per ever-seen `chat_id` for the lifetime of the process.

Naïve fix: drop the per-chat state in `unregister_socket` when the chat has
no more sockets. **That breaks the very feature the rings exist for** —
the replay test scenario is exactly: register, send, unregister, register
with `since_seq=N`, expect replay.

## Decision

**Defer cleanup by `replay_ttl_seconds` (default 300s = 5 minutes).**

When the last socket for a `chat_id` unregisters:

1. Schedule an `asyncio.create_task(_evict_after_ttl(chat_id))`.
2. The task sleeps `replay_ttl_seconds`, then drops `_replay_buffers[chat_id]`
   and `_outbound_seq[chat_id]` if no socket has rejoined.

When a socket registers for a `chat_id`:

1. If a pending eviction task exists for that `chat_id`, cancel it.
2. Replay (if `since_seq` is set) sees the intact ring.

`adapter.disconnect()` cancels every pending task so they don't outlive the
adapter.

## Consequences

**Positive:**

- Memory bounded by the active-or-recent set of `chat_id`s, not the
  ever-seen set. A long-running arcui process no longer leaks.
- Reconnects within the grace window (typical browser tab refresh, brief
  network blip, mobile-app sleep cycle) get full replay — feature preserved.
- Audit emits `gateway.replay.evicted` so operators can see the eviction in
  the trace dashboard.

**Negative:**

- Reconnects *after* `replay_ttl_seconds` see no replay — but this is fine,
  the agent's `SessionManager` still has the durable JSONL history that the
  browser fetches via `/api/agents/{id}/sessions/{sid}` on reconnect.
- One pending `asyncio.Task` per recently-disconnected `chat_id`; task body
  is a single sleep, so the cost is negligible.

**Trade-off rejected:**

- *Permanent retention* (the original implementation) — leaks unboundedly.
- *Synchronous cleanup on last unregister* — breaks the replay-after-disconnect
  contract.
- *LRU cap on chat_id count* — adds a constant to tune; TTL is simpler and
  matches operator intuition ("after a coffee break the chat is gone").

## Configuration

`replay_ttl_seconds` is a constructor parameter on `WebPlatformAdapter`,
default `300.0` from `_DEFAULT_REPLAY_TTL_SECONDS`. Tests use `0.01` for
fast eviction; production uses the default.

## Verification

- `tests/unit/test_web_adapter.py::test_replay_buffer_evicted_after_ttl_elapses`
- `tests/unit/test_web_adapter.py::test_replay_buffer_survives_within_ttl_window`
- `tests/unit/test_web_adapter.py::test_register_within_ttl_cancels_pending_eviction`
- `tests/unit/test_web_adapter.py::test_disconnect_cancels_pending_evictions`

## Code anchors

- `packages/arcgateway/src/arcgateway/adapters/web.py`:
  `_DEFAULT_REPLAY_TTL_SECONDS`, `_eviction_tasks`, `_schedule_eviction`,
  `_evict_after_ttl`.
