# SPEC-055 — Mention-scoped inbox activation (relevance triage)

**Status:** DRAFT · **Owner:** olivia hardening session · **Created:** 2026-07-12
**Package(s):** arcagent (messaging module) · **Depends on:** MSG5/6/7 (deployed)

## Problem

Every channel member spins a **full LLM run** on **every** channel message, even
when the message is not addressed to it.

**Live evidence (2026-07-11, olivia/DGX):** posting one `@josh_agent` mention to
`#brand` caused **all 4 agents** to run a `messaging:inbox` turn (4× `loop.checkpoint`).
Three of them burned a full Sonnet turn only to decide "not for me." This is
Anthropic's documented ~15×-token multi-agent anti-pattern
([[reference_claude_multiagent_patterns]]).

### Current behavior (root)

`packages/arcagent/src/arcagent/modules/messaging/capabilities.py`:
- `_handle_incoming(message)` **always** routes a pushed message into a run via
  `deliver_fn` (steer/follow_up) or `agent_run_fn`.
- `_interrupt_for(msg, identity)` only decides **steer vs. follow_up timing**
  (critical → interrupt; `action_required` + @mentions-me → interrupt; else
  follow_up). It never decides **whether a run happens at all**.

So the @mention is an *interrupt* signal, not an *activation* signal.

## Requirements

- **R1 — Mention scopes activation.** A channel message that @mentions specific
  entities wakes a run **only** for mentioned members. A non-mentioned member does
  **not** run.
- **R2 — Unaddressed broadcast wakes all.** A channel message with **no** mentions
  wakes every member; each decides goal/agent-relevance **in-run** and answers only
  if relevant (today's behavior, preserved). This is the "team question" case.
- **R3 — DMs always wake the recipient.** A direct message (delivered only to the
  addressed agent's `arc.agent.{handle}` inbox) always wakes it.
- **R4 — Critical always wakes.** `priority == critical` overrides the gate
  (safety / kill-switch traffic must never be silently dropped).
- **R5 — No lost history.** A non-woken channel message is **not** copied into the
  agent's `messaging:inbox` session, but remains in the channel stream
  (NATS + arcstore directory) and is retrievable on demand. The channel stream is
  the record; the agent does not need a private copy of traffic it ignored.

## Design

Single activation predicate at the receiving agent, evaluated **before** the run
is woken. Concern separation is preserved: **arcteam still delivers** every
subscribed message; **arcagent** decides whether its own inbox wakes a run.

```
def _should_activate(msg, identity) -> bool:
    if str(msg.priority) == "critical":          # R4
        return True
    if not msg.mentions:                          # R2 broadcast / R3 DM (only I receive it)
        return True
    return identity is not None and identity.did in list(msg.mentions)   # R1
```

`_handle_incoming`:
- `if not _should_activate(message, st.identity):` → **ack + return** (no
  `deliver_fn`, no `agent_run_fn`, no follow_up queued). The message stays in the
  channel stream (R5).
- else → existing path (`deliver_fn` with `interrupt=_interrupt_for(...)`, or
  `agent_run_fn`).

Why the predicate is correct for every delivery shape:
- **DM** → only the addressed agent receives it at all; `mentions` empty → wakes it (R3). ✓
- **Channel broadcast** (no mentions) → all members receive it → all wake (R2). ✓
- **Channel targeted** (`mentions=[A]`) → all members receive it on the stream, but
  only A satisfies the predicate → only A wakes (R1); B/C/D ack-and-ignore. ✓
- **Critical** → wakes regardless (R4). ✓

### Open decision for Josh
- **R5**: default is "don't copy ignored channel traffic into the agent's inbox
  session." Alternative = silently append (no run) so a later relevant turn has
  local context. Default chosen for simplicity + lower context bloat; the channel
  stream already preserves everything. Confirm.

## Test plan (TDD)

Unit (`packages/arcagent/tests/unit/modules/messaging/`):
1. channel msg `mentions=[A]` → `_should_activate` True for A, **False** for B.
2. channel msg `mentions=[]` → True for all members.
3. DM (mentions empty, delivered to A only) → True for A.
4. `priority=critical`, `mentions=[B]`, evaluated as A → True (R4 override).
5. `_handle_incoming` with a non-activating msg → neither `deliver_fn` nor
   `agent_run_fn` is called, message is acked (no `RetryableDeliveryError`).

Live (olivia): post `@josh_agent …` to `#brand` → **exactly 1** `loop.checkpoint`
(josh_agent only), not 4. Post an un-mentioned question → all 4 consider it.

## Tasks

- [ ] T1 — `_should_activate` predicate + unit tests (R1–R4). *(TDD: tests first)*
- [ ] T2 — Gate `_handle_incoming` on it; ack-and-ignore non-activating (R5 default).
- [ ] T3 — Confirm ack semantics don't trip `RetryableDeliveryError` / DLQ.
- [ ] T4 — ruff + mypy --strict + full arcagent messaging suite.
- [ ] T5 — Deploy to olivia; live-verify 1-run-not-4; update BACKLOG.

## Non-goals
- Goal-relevance *scoring* for the broadcast case (R2 keeps the in-run LLM decision).
  A cheaper pre-LLM relevance classifier is a later optimization, not this spec.
- Coordinator / task-list / completion-verification (separate — see roadmap note).
