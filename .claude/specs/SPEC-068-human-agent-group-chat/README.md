# SPEC-068 — Human + agent group chat in Team Chat

| | |
|---|---|
| **Status** | **APPROVED — D1 decided by the owner (2026-08-17), implementing in three stages.** |
| **Branch** | worktree off `develop` |
| **Type** | integration |
| **Package(s)** | `arcteam` (channels, routing), `arcagent` (activation), `arcui` (compose surface) |
| **Depends on** | SPEC-055 (shipped), SPEC-031 F1/F2 (`/ws/team`), SPEC-026, SPEC-022, `7812264f` |
| **Proposed decisions** | D-683 – D-689 (not yet written to `.claude/decisions-log.md`) |

---

## The premise was wrong, and the correction is the most useful thing in this document

The brief states that Team Chat is a read-only observation window, that nothing
consumes the compose box, and that the human's posts never reach arcteam.
**All three are false on `develop`.** The write path exists, is wired, and works.

Verified end to end in the tree:

| Step | Where | Evidence |
|---|---|---|
| Compose box sends | `packages/arcui/web/src/hooks/use-team-stream.ts:94` | `ws.send(JSON.stringify({ type: 'post', channel, text }))` |
| Socket accepts the post | `packages/arcui/src/arcui/routes/team_ws.py:40-78` | `_receive_from_browser` → `forwarder(sender=…, channel=…, text=…)` |
| Forwarder is wired at boot | `packages/arcui/src/arcui/server.py:412-416` | builds it when a messaging service resolved |
| Human gets a signing identity | `packages/arcui/src/arcui/messaging.py:132-158` | `_operator_messaging()` derives a DID + `MessageSigner` from the operator key |
| Human is a registered entity | `packages/arcui/src/arcui/messaging.py:233-243` | self-registers `user://operator`, `EntityType.USER`, with `public_key` |
| Message is signed and routed | `packages/arcteam/src/arcteam/messenger.py:300-415` | `send()` — signs, enforces membership, appends to `arc.channel.<name>` |
| Agents subscribe to channels | `packages/arcteam/src/arcteam/messenger.py:586-634` | durable consumer per member channel |
| Inbound wakes a run | `packages/arcagent/.../messaging/capabilities.py:233-289` | `_handle_incoming` → `deliver_fn` / `agent_run_fn` |
| Reply returns to the channel | `packages/arcagent/.../messaging/capabilities.py:202-215, 405-424` | `_origin_reply_target` → `_send_to_team` |

The docstring the brief quotes — *"Both routes are read-only; sending messages
remains the responsibility of agents themselves"* — is **stale prose in
`team_chat.py`**, scoped to that module's own two GET routes and already false
even there (the same file has had operator-only `POST /api/team/channels` and
member add/remove since COMP-005). It describes an earlier world. It is not the
mechanism.

Likewise `mentions.py`'s *"best-effort attention hints, not routing"* is stale.
Mentions **are** routing today, deliberately and in two independent places:

- `messenger.py:405-463` — `_fanout_mentions_to_inboxes` copies the envelope into
  each mentioned entity's `arc.agent.<handle>` inbox *specifically so the mention
  wakes it regardless of channel membership or subscribe timing*.
- `capabilities.py:129-143` — `_should_activate` makes a mention the **scope** of
  activation: a channel message naming other agents wakes only those agents.

The surviving truth in that sentence is narrow and it matters: an `@handle` that
resolves to **no registered entity** is silently treated as plain text
(`mentions.py:41-43`, `continue` on `UnknownHandle`). That is the real defect
behind the brief's four-day-old `@sales_agent` post — not a routing philosophy.

**The journal evidence was misread.** `/ws/team` posts are WebSocket frames.
A WebSocket frame is never an HTTP `POST` and will never appear as one in a
service journal. "No message POST in the journal, ever" is exactly what a
correctly working forwarder looks like. The brief's own counter-observation —
that the messages *persisted* and are re-served by `GET .../messages` — is
decisive and points the other way: `MessagingService.send` **succeeded**. The
operator entity registered, the channel membership check passed, the envelope was
Ed25519-signed, and the row landed on `arc.channel.work`.

So the failure is not on the write path. It is downstream, and it is four
specific defects.

---

## Why six online agents said nothing

### F1 — A dashboard-created channel has exactly one member: the operator

`messaging.py:246-249`:

```python
existing = next((c for c in channels if c.name == channel), None)
if existing is None:
    await service.create_channel(Channel(name=channel, members=[op.did]))
```

Auto-create is documented as the convenience that "makes a fresh channel
reachable from the dashboard without a separate `arc team` round trip". It
creates a channel whose membership is `[operator]`. **No agent is a member, so no
agent subscribes, so no agent receives anything.** The operator sees his own
posts echo back through `TeamBusObserver` and reads a room he is alone in.

This is the highest-probability cause of the live symptom and it is a one-member
channel masquerading as a team room.

### F2 — Channel subscriptions are snapshotted once, at agent startup

`messenger.py:610-617` resolves member channels **inside** `subscribe()`, and
`capabilities.py:775` calls `subscribe()` exactly once, from a background task
that then blocks on `subscription.wait()` forever.

A channel created after an agent booted, or an agent added to a channel after it
booted, is **never subscribed until that agent restarts**. Nothing re-resolves.
This compounds F1: even fixing membership does not deliver anything to a running
agent.

### F3 — The client discards every error frame the server sends it

`use-team-stream.ts:56-72` handles `type === 'ready'` and `type === 'team_message'`.
Nothing else. The server's `forward_unavailable`, `forward_failed`,
`missing_channel`, `empty`, `malformed`, and the `posted` acknowledgement
(`team_ws.py:54-78`) are all parsed and then dropped on the floor.

`messages.tsx:260-264` compounds it:

```ts
const send = () => {
  if (!text.trim()) return
  post(text)
  setText('')          // cleared unconditionally, before any ack
}
```

And `post()` itself (`use-team-stream.ts:93`) silently returns when the socket is
not `OPEN`. **This is the "worse than a visible error" the brief describes, and it
is a client-side bug of about ten lines.** The server is honest; the browser
throws away what it says.

### F4 — An unresolvable `@handle` vanishes

`mentions.py:41-43`. `@sales_agent` when the registry holds `sales` produces
`mentions = []`, which downgrades the message to an un-addressed broadcast: no
`action_required`, no `HIGH` priority, no inbox fanout. The human typed an
address and got a broadcast, with no feedback that the name was wrong.

---

## What "make it a group chat" actually requires

Given the above, this spec is **not** "build a write path". It is:

1. Make channel membership mean what an operator thinks it means (F1, F2).
2. Make the compose box tell the truth (F3, F4).
3. Decide who answers, and bound what it costs (D1, D5).
4. Close the loop hole that today has **no guard at all** (D4).

---

## Decisions

### D1 — Who answers *(DECIDED by the owner, 2026-08-17)*

> *"we can't do designated responder. sometimes I don't know who has the answer,
> and they need to assess if the message is for them, or they have something
> valuable to add or not."*

**Relevance triage is a product requirement, not a cost compromise.** A
designated responder is rejected: it assumes the human knows who owns the answer,
and the whole reason to post in a channel rather than a DM is that he does not.

Two distinct judgements, per agent, and the second is not a weaker form of the
first:

1. **Is this addressed to me, or in my domain?**
2. **Do I have something valuable to add?**

An agent holding genuinely useful context should speak up even when the message
was not aimed at it. That is the behaviour being bought.

**My earlier objection was aimed at the wrong target.** The problem was never
that agents judge. It is that the judgement **failed open** — and a cost control
whose failure mode is "spend the maximum" is the wrong shape regardless of who is
judging. So the judgement stays and the failure direction inverts.

#### D1a — The gate fails CLOSED

`_passes_channel_triage` currently returns `True` on a missing classifier, any
exception, and any non-`N` verdict including empty and garbled
(`capabilities.py:166-184`). Every one of those becomes silence.

| Gate outcome | Today | After |
|---|---|---|
| explicit YES | wake | wake |
| explicit NO | skip | skip |
| empty / garbled | **wake** | **skip** |
| exception / timeout | **wake** | **skip** |
| no classifier bound | **wake** | **skip** |

The trade, stated plainly: fail-closed can drop a message an agent should have
answered. That failure is **visible** (the human sees no reply and can `@` the
agent, which bypasses the gate entirely and is guaranteed to wake it) and it is
**cheap**. Fail-open's failure is N full runs on every message, silently, forever
— invisible until the bill. A mention is always available as the deterministic
override, which is what makes fail-closed safe to choose here.

#### D1b — The gate must be a rounding error

The gate decides *whether to pay for a reply*. If the gate costs a meaningful
fraction of the reply, it has no reason to exist.

- **One `quick_classify` call** — one `arcllm` invoke, no tools, not the agentic
  loop (`agent.py:732-749`). Already the right primitive.
- **`max_tokens = 8`.** The answer is one word.
- **Hard timeout, 5 s**, via `asyncio.wait_for`. There is no timeout today, so a
  hung provider currently blocks the inbox consumer indefinitely. On timeout:
  silence (D1a).
- **A small fast model** — *not shipped, and deliberately not faked.* This is the
  single biggest cost lever, but it is not one config line: `quick_classify`
  calls `self._ensure_model()`, a single per-agent cached provider instance
  bound to one resolved model name, and `invoke()` has no per-call model
  override (`arcllm/types.py:193`). Honouring `triage_model` means resolving a
  second provider instance through `arcrun`, which is a new seam in a lower
  layer. **Proposed rather than half-wired**, because a config field that reads
  as a cost control and silently does nothing is worse than its absence. Until
  it exists the gate runs on the agent's own model, bounded to 8 output tokens
  and 5 seconds.
- **Body truncated to 2000 chars** (already the case) and sanitised (LLM01).

Prompt shape — it must ask **both** of Josh's judgements, and must make silence
the default rather than a fallback:

```text
system: You are {name}, one member of a team, in the shared channel #{channel}.
        Your role: {role}.
        A message was posted to the channel. It was not addressed to anyone
        specific.
        Answer YES only if either is true:
          1. The message is about your role or domain.
          2. You hold specific information that would genuinely help, that
             another member is unlikely to have.
        Adding agreement, encouragement, or a restatement is not value.
        If you are unsure, answer NO.
        Reply with exactly one word: YES or NO.
user:   {sender_handle}: {body}
```

Two deliberate properties: naming the role makes judgement 1 answerable, and
"unsure ⇒ NO" plus the explicit non-value clause is what stops six agents
answering "thanks".

#### D1c — Blast radius: at most 2 answers, 60 s cooldown

**Answer cap = 2 per message.** Justification: a cap of 1 is a designated
responder chosen by a race, which is the thing the owner rejected. At 3+, a
six-agent channel reads as a pile-on in a chat window and marginal value falls
away faster than cost does. Two is exactly the shape the owner described — the
agent who owns it answers, and one other agent with genuinely different context
adds to it.

Mechanism: before waking, an agent reads the message's thread via the existing
`MessagingService.get_thread(stream, thread_id)` and stays silent if two replies
already exist. **This is a soft cap** — agents decide independently with no
coordinator, so C agents deciding concurrently can overshoot to C. Stated
honestly rather than papered over: the hard bounds are the token budget and the
breaker below. Mentions and `critical` bypass the cap, because an explicitly
addressed agent must never be silenced by someone else having spoken first.

**Cooldown = 60 s per (agent, channel).** Justification: it matches
conversational turn granularity. It stops one agent answering every message in a
rapid exchange while leaving a normal back-and-forth intact, and it damps the
reply-to-reply case that D4 bounds structurally. Mentions and `critical` bypass
it, for the same reason.

#### D1d — Check order is cheapest-first

Each check is skipped only if a cheaper one already decided. The LLM call is last.

| # | Check | Cost | Bypassed by |
|---|---|---|---|
| 1 | self-suppression (`signer_did == my did`) | free | nothing |
| 2 | hop budget (`hop >= 2`) | free | nothing |
| 3 | mention scope (SPEC-055) | free | — *decides* |
| 4 | sender is human (`EntityType.USER`) | cached roster | mention, critical |
| 5 | cooldown | free, local | mention, critical |
| 6 | answer cap | one stream read | mention, critical |
| 7 | circuit breaker | free, local | nothing |
| 8 | **relevance gate (LLM)** | ~8 output tokens | mention, critical |
| 9 | full run | the real cost | — |

The resulting ladder:

| Message shape | Who wakes | Cost |
|---|---|---|
| `priority == critical` | every member | N full runs |
| `@mentions` present | only the named agents (+ guaranteed inbox fanout) | M full runs, M = names |
| no mentions, **human**-authored | members passing the fail-closed gate, capped at 2 | N × 8-token calls + ≤2 full runs |
| no mentions, **agent**-authored | nobody (D4a) | 0 |
| DM | the addressee | 1 full run |

The mention tier is unchanged and I am **not** proposing to change it. The brief
asked whether "hints, not routing" was a deliberate decision being overridden. It
was a decision, it was already reversed by SPEC-055 on recorded live evidence
(one `@josh_agent` post to `#brand` burned a full Sonnet turn in four agents),
and that reversal was correct. The stale docstring goes; the behaviour stays.

A mention is also what makes D1a's fail-closed gate safe: it is the deterministic
override a human always has when the gate guesses wrong.

### D2 — How an agent sees the channel

**Reuse `MessagingService.subscribe`. No new seam, no polling, no new inbox.**

The durable-consumer model already gives exactly the required properties: a
running agent is pushed to live, a restarted agent resumes from its last ack
(`messenger.py:586-634`), every delivery is Ed25519-verified with sender-binding
(`_verify_origin`, `messenger.py:556-582`), fan-out duplicates are deduped by
message id, and backpressure defers rather than drops
(`RetryableDeliveryError`, `_dispatch:701-707`). Building anything else would be
a second messaging system.

**The one change required is F2: make the subscription set live.** `subscribe()`
must re-resolve the entity's channel membership periodically and open or close
consumers to match, rather than snapshotting once. This is entirely internal to
`arcteam.messenger` — the `Subscription` object already owns a task set, so the
change is a supervisor tick over the same list, not a new contract. **No package
boundary moves.**

### D3 — Ambient context, and the `7812264f` rule

The rule is precise and I am honouring it exactly as written: **an agent may
answer what it overhears, but must not remember it.**

The mechanism is already built and must not be re-invented:

- `turn_context.set_overheard(bool)` — a `contextvars.ContextVar` bound at
  dispatch entry alongside the inbound channel, for the documented reason that a
  contextvar set across the executor→agent task boundary does not reliably reach
  the loop's hooks (`turn_context.py:31-54`).
- `capabilities.py:275` passes `overheard=is_channel_broadcast`.
- `modules/memory/capabilities.py:213` — the `agent:pre_respond` capture hook
  returns early when `turn_context.overheard()`. Replying, tools, and the
  response are untouched. Only retention is suppressed.

**What this spec adds: ambient context is a pull, never a push.**

An agent that was not addressed does not run at all, so it has no ambient
context — which is the correct trade and is why the channel stream, not the
agent, is the record (SPEC-055 R5). When an agent *is* later addressed and needs
to know what the room was discussing, it reads the channel on demand through the
tools it already has (`messaging_read_thread`, `messaging_check_inbox`). That
read enters the turn's working context and dies with the turn.

The invariant, stated so it can be tested: **channel traffic may enter a prompt;
it may never enter `Brain` capture.** Any new path that puts channel history in
front of a model must set `overheard=True` unless the message named this agent.
A test asserting the *delivery call* carries the flag — not just that the
predicate computes it — already exists
(`tests/unit/modules/test_overheard_not_retained.py`), and the commit message
explains why that distinction is the one that catches this repo's recurring bug
shape. Extend that test, do not replace it.

Deliberately excluded: pushing un-addressed channel history into every member's
session. It is the alternative SPEC-055 R5 flagged for confirmation and it is the
exact thing `7812264f` was written to stop.

### D4 — Loop prevention

**Today there is no loop guard of any kind.** `_should_activate` returns `True`
for any un-addressed message including one written by another agent — and an
agent's reply *is* an un-addressed channel post (`_send_to_team`,
`capabilities.py:405-424`, sends `to=[channel]` with no mentions). A → B → A is
reachable now; the only thing standing between the fleet and a runaway is the
fail-open triage classifier's opinion. That is not a guard.

Four mechanisms, in order of strength:

**(a) Only a human-authored un-addressed post fans out.** An un-addressed channel
message whose sender resolves to `EntityType.AGENT` does not activate other
agents. This makes the fan-out loop **structurally impossible** rather than
merely bounded, and it costs one registry lookup the send path already performs.
An agent that genuinely needs a teammate must `@mention` it — which is explicit
addressing, is already how the roster prompt instructs agents to behave
(`capabilities.py:343-348`), and is consistent with arcteam's standing rule that
messaging is wake-signal and narration, never the carrier of work (REQ-251).

**(b) A hop budget bounds the addressed chain.** `Message.hop: int = 0`.
Human-authored posts are 0; a message an agent sends from within a woken turn
carries `parent.hop + 1`. At `hop >= 2` the message is stored and readable but
does not activate. This bounds a mention chain A→B→A→B to a fixed depth.

> **`hop` must be a typed field on `Message` and must be added to
> `crypto._SIGNED_FIELDS`.** It cannot live in `meta`: `_SIGNED_FIELDS`
> (`crypto.py:24-38`) does not cover `meta`, so a hop counter kept there is
> unsigned and an agent could reset it to 0 and re-arm the loop it is in. A loop
> guard an adversary can clear is not a guard.

**(c) Explicit self-suppression.** An agent must not activate on its own message.
`_fanout_mentions_to_inboxes` already skips the sender, but the **channel stream
copy still reaches the sender's own consumer**. Add `signer_did == identity.did →
return` at the top of `_handle_incoming`.

**(d) Per-(agent, channel) circuit breaker.** Reuse
`arcagent.modules.proactive.circuit_breaker.CircuitBreaker` — already a
Resilience4j state machine with exponential backoff capped at 30 minutes
(`circuit_breaker.py:1-60`). More than *k* activations from one channel inside a
window trips it OPEN; that channel stops waking that agent; backoff doubles per
reopen. Every trip is an audit event, so a runaway is visible rather than
inferred from a bill.

(a) and (c) are load-bearing. (b) and (d) are the belt.

### D5 — Cost

**The ceiling for an N-member channel, after this spec:**

| Case | Gate calls | Full runs |
|---|---|---|
| Human `@mentions` M agents | 0 | M |
| Human un-addressed | N × 8 output tokens | **≤ 2** (soft cap, D1c) |
| Human un-addressed, gate broken | N failed calls, then breaker OPEN | **0** |
| Agent reply (D4a) | 0 | 0 |
| *Today, un-addressed, gate broken* | *N failed* | ***N*** |

The last row is what this spec deletes. The gate's own cost is bounded by
`max_tokens=8` and a 5 s timeout, so it is a rounding error against a full
agentic turn — which is the only thing that justifies running it N times.

Controls engaged, all existing primitives:

- **`CircuitBreaker`** (`arcagent/modules/proactive/circuit_breaker.py`) —
  per `(agent, channel)`, `failure_threshold=5`, `base_wait=30 s`,
  `max_wait=1800 s`. A gate that keeps failing stops being called at all, with
  exponential backoff. Chosen over a bespoke limiter because it is already the
  repo's resilience primitive and already tested.
- **Cooldown and answer cap** — D1c.
- Every activation decision is audited as `messaging.activation` with its
  reason, so "nobody answered" is a queryable fact rather than an inference.

> **Correction found while implementing: `RootTokenBudget` cannot be the
> cross-agent bound, and claiming it would have been false.** It is an
> in-process `asyncio.Lock` over one integer
> (`arcagent/orchestration/token_budget.py`), built for a root run and the
> children it spawns *inside the same process*. Agents in a fleet are separate
> processes and share nothing but the bus, so there is no in-process budget for
> a channel post to open across six of them. Wiring it here would have produced
> a per-agent counter that looks like a fleet-wide ceiling and is not — the
> exact class of defect this spec exists to remove.
>
> So the honest split is: the **cross-agent** bound is the answer cap, which
> coordinates through the one thing agents genuinely share (the channel
> stream), and is soft for that reason. The **per-agent** bounds are the
> cooldown and the breaker, which are hard. A real fleet-wide token ceiling
> needs a distributed counter on the bus and is a separate piece of work.

No parallel limiter is introduced.
---

## Seams — nothing new, no arrow reversed

| Piece | Package that owns it | Why it is legal |
|---|---|---|
| `Channel.responder`, `Message.hop` | `arcteam` | arcteam owns channels and the envelope |
| Live re-resolve of subscriptions | `arcteam.messenger` | internal to `subscribe()`; contract unchanged |
| Activation predicate, hop check, self-suppression | `arcagent.modules.messaging` | the receiving agent decides whether *it* wakes — the SPEC-055 split, preserved |
| Overheard flag | `arcagent.core.turn_context` | already there; extend, don't move |
| Budget / breaker | `arcagent.orchestration`, `arcagent.modules.proactive` | already there |
| Setting a responder | `arcui/routes/team_chat.py` | operator-only + `emit_mutation_audit`, exactly like member add/remove |
| Showing acks and errors | `arcui/web/` | client-side only |

Checked against the enforced rules:

- **`arcagent` imports neither `arcgateway` nor `arcui`.** Nothing here asks it
  to. Activation is decided inside `arcagent` from an `arcteam` envelope.
- **`arcui` does not become an agent-push telemetry pipeline (SPEC-026).**
  `/ws/team` already exists and is a *view over the bus* fed by the read-only
  `TeamBusObserver`, plus a one-way forward. No agent pushes into arcui.
- **`arcui` never touches `team/` (SPEC-022).** No filesystem access is proposed.
- **Root facade only.** `arcui` reaches arcteam through `arcteam.*` public names
  as it already does; nothing reaches past a neighbour.
- **`arcui` never signs or routes.** It hands `(sender, channel, text)` to a
  forwarder; `MessagingService.send` signs. Preserved as-is.
- **ASI09 — agents never impersonate humans.** The human posts as
  `user://operator`, `EntityType.USER`, under the operator key. Agent posts carry
  agent DIDs. D4(a) depends on that distinction being real, which it already is.

**One seam I considered and rejected:** having `arcui` decide who should answer
and address the message accordingly. It is tempting because the dashboard knows
the channel. It is wrong — it would make the dashboard a router, put activation
policy in the surface layer, and mean a message posted from the CLI or another
agent obeyed different rules than one posted from the browser. Activation belongs
to the receiving agent. One rule, one place.

---

## Failure modes

| Failure | Behaviour | Why that is the right trade |
|---|---|---|
| Operator key absent | forwarder is `None`; `forward_unavailable` frame | degrade loudly; arcui must never mint a signing authority |
| Broker down | `team_messaging_unavailable` (503) | already correct — never a fabricated empty list |
| Classifier down (option A) | fail-open, wake, audit, count toward breaker | a relevant message is never silently dropped; the breaker caps the bill |
| Responder agent offline (option B) | message sits durably in the channel; delivered on its next subscribe | durable consumers already resume from last ack |
| `@handle` does not resolve | **must become a visible error to the human** | today it silently downgrades to a broadcast — F4 |
| Agent steering queue full | `RetryableDeliveryError`, not acked, redelivered | already correct |
| Two agents share a DID or workspace | `arc up --check` refuses the fleet | shipped in `7812264f`; a shared DID would let one agent's memory absorb another's |
| Budget exhausted | further activation refused, audited | refusing a run beats truncating one |
| Breaker OPEN on a channel | that agent stops waking there; exponential backoff; audited | contains blast radius without killing the agent |

---

## Deliberately excluded

- **Changing the mention tier.** It works and SPEC-055 justified it on live
  evidence. Only the stale docstring goes.
- **Pushing un-addressed channel history into member sessions or memory.** Direct
  violation of `7812264f`.
- **Human-to-human DMs / multi-user identities.** The dashboard has one operator
  identity derived from one deployment key. Per-viewer human identities are a
  real feature and a different spec — the viewer-token DID is already kept as
  unsigned attribution in `meta.operator_token_did` (`messaging.py:250-258`),
  which is the honest placeholder.
- **Typing indicators, read receipts, reactions, threads-as-UI.** Slack-shaped
  polish; none of it is why nobody answered.
- **A cheaper pre-LLM relevance classifier.** SPEC-055 named it a later
  optimisation and that is still right; D1 option B removes most of the need.
- **Streaming an agent's reply token-by-token into the channel.** The channel is
  a message log, not a turn stream. `/ws/chat/{agent_id}` is where live turns
  live.

---

## Implementation stages

Ordered so that each stage makes the next one debuggable. Full gates every
stage: `ruff check`, `mypy --strict`, and the **full** suite — not a subset.

| Stage | Ships | Why this order |
|---|---|---|
| **1** | F3 — client surfaces `posted` / every error frame; compose box only clears on ack | ~10 lines, and it converts every remaining failure from silent to visible |
| **2** | F1 + F2 + F4 — channel membership usable from the UI, live re-resolve of subscriptions, loud unresolvable-`@handle`; delete the stale `team_chat.py` docstring | membership must be real before "who answers" can be observed at all |
| **3** | D1 (fail-closed gate, timeout, cap, cooldown) + D4 (human-only fan-out, signed `hop`, self-suppression) + D5 (budget, breaker) | the expensive, judgement-bearing layer, on top of a stack that now reports its own failures |

## Open questions for the owner

1. ~~**D1 — un-addressed posts.**~~ **Decided 2026-08-17: relevance triage,
   fail-closed. Designated responder rejected.**
2. ~~**Auto-create on first post.**~~ **Settled during stage 2, and I changed my
   own recommendation.** I had proposed refusing a post to a channel that does
   not exist. Reading the code that decides it, that is wrong: auto-create is a
   deliberate, documented convenience for a trusted operator (REQ-061), and
   `test_post_to_new_channel_creates_it` pins it — a test pinning a behaviour is
   a message to whoever removes it. The defect was never the creation, it is
   that the resulting room has no agent in it and says nothing about that.
   So auto-create stays and the post is **delivered with a warning** naming the
   empty channel. Membership management already exists in the dashboard
   (`components/channel-management.tsx`), so the warning points at a control the
   operator already has. Refusing would have deleted a justified feature to fix
   a reporting bug.
3. **Should a channel default to every registered agent?** Convenient, and it
   matches "the team channel"; also the largest fan-out surface. Left to the
   operator per channel rather than defaulted.

---

## Follow-ups this implementation created

- **`entity_role` has no default source.** The gate's first judgement is "is this
  about your role?", which needs a role. It is a new config line and an unset one
  renders as "not stated", which weakens judgement 1 without breaking it. The
  obvious source is the agent's registered `Entity.roles`; wiring that means
  threading the entity snapshot the ladder already fetches for the human check
  into the prompt builder. Worth doing, not worth blocking on.
- **`triage_model`** — see D1b. Needs an `arcrun`/`arcllm` seam for a second
  resolved provider instance.
- **A fleet-wide token ceiling** — see D5. Needs a distributed counter on the bus.

## Also found (out of scope, flagged not fixed)

- **`classification` is not in `crypto._SIGNED_FIELDS`** (`crypto.py:24-38`).
  It is enforced on send (`_enforce_no_write_down`) but not covered by the
  signature, so it is not tamper-evident in transit the way `body` and `mentions`
  are. **Raised as its own spec at the owner's direction — deliberately not
  folded in here**, because it is a SPEC-038 classification-integrity question
  and not a group-chat question, and bundling it would hide it inside a UI
  change. This spec adds `hop` to `_SIGNED_FIELDS` and touches nothing else in
  that tuple.
- **`team_chat.py`'s module docstring** describes the file as read-only while the
  same file exposes three operator-only mutation routes. It sent this
  investigation down the wrong path and should be rewritten regardless of what
  happens to this spec.
