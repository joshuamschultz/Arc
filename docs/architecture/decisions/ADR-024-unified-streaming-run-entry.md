# ADR-024: One Streaming, Session-Bound `agent.run` — Every Surface Goes Through arcrun

**Status**: Accepted
**Date**: 2026-05-31
**Builds on**: ADR-020 (arcgateway as Data Plane), ADR-023 (Capability Resolution)
**Supersedes**: the `agent.chat` / `agent.run` / `agent.run_async` / `agent.run_stream` split; the executor's non-streaming "wrap the whole reply as one token" path (`executor.py` M2 TODO)

## Context

arcrun is the execution loop and the single runtime path to arcllm. Routing is
already unified — chat from any channel goes `adapter → SessionRouter →
executor → agent → arcrun`, and the CLI goes `agent.run → arcrun`. But the
**agent entry surface is forked four ways** and chat doesn't really stream:

| Surface | Entry | Reality |
|---|---|---|
| Chat (gateway) | `agent.chat(msg, session_id=)` | executor wraps the whole reply as one fake "token" (M2 TODO) |
| CLI / fallback | `agent.run(msg)` | one-shot, no session |
| Async | `agent.run_async()` | handle |
| Streaming | `agent.run_stream()` | real arcrun event stream — but the gateway never uses it |

`agent_dispatch` builds the tool list three times. Four methods, a real stream
that isn't wired, and a chat path that pretends to stream.

## Decision

**One entry. Session-bound. Always streaming.**

```python
async def run(self, input, *, session: Session) -> AsyncIterator[StreamEvent]:
    """The only way to drive an agent. Always bound to a session; always
    streams arcrun events (TokenEvent … TurnEndEvent). One-shot callers collect
    the stream to a result."""
```

1. **Everything needs a session → there is no separate `chat`.** `run` carries
   everything `chat` had (the session / channel context, history append via the
   agent's `SessionManager`). A session is mandatory; callers that don't have one
   (CLI, scheduler) open/resume a deterministic local session. Channel-specific
   sessions stay first-class (a Slack thread and a UI thread are distinct
   sessions — ADR-020 / arcgateway session model).
2. **All surfaces stream — agent, chat, CLI, scheduler, MAS.** `run` returns an
   async iterator of arcrun `StreamEvent`s. The gateway executor consumes that
   iterator and emits real `Delta`s token-by-token (finishing the M2 TODO). A
   one-shot caller wraps the stream with a small `collect()` to get a final
   result. **Accepted trade-off:** one-shot callers pay a collect wrapper; worth
   it for a single code path.
3. **`run_async` / `run_stream` / `chat` are deleted**, along with the duplicate
   dispatch in `agent_dispatch` and the non-streaming branch in the executor.
4. **It carries the unified `CapabilityProvider` (ADR-023)** into `arcrun.run`,
   not the flat `to_arcrun_tools()` list.

## Consequences

**Positive**
- One way to run an agent, from any surface. Chat streams for real. The
  capability contract (ADR-023) and the durable record (SPEC-026) flow through
  one path, so Observe/audit stay correct with no special-casing.
- Less core surface in the arcagent nucleus (helps the <3500 LOC budget): four
  entry methods → one; the executor's chat/run fork → gone.

**Negative / accepted**
- One-shot callers (CLI, scheduler) collect a stream they don't visually need —
  a thin wrapper, accepted (decision 2).
- A mandatory session means even a trivial CLI call opens/resumes a local
  session. Accepted: sessions are cheap and make history/audit uniform.

**Open (for the spec)**
- Exact `Session` object the CLI/scheduler pass (open-or-resume a deterministic
  local session key).
- Back-compat is not a concern (local-only repo) — delete the old methods in the
  same change, no shims.

## Alternatives considered

- **Keep `run` (one-shot) and `run_stream` (streaming) as two methods.** Less
  churn, but perpetuates the fork and the fake-streaming chat path. Rejected —
  the whole point is one entry.
- **Optional session.** Simpler signatures, but then history/audit/channel
  behavior diverges between "has session" and "no session" callers. Rejected —
  mandatory session keeps every surface identical.
