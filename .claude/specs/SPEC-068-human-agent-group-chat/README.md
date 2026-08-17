# SPEC-068 — Human + agent group chat in Team Chat

| | |
|---|---|
| **Status** | **DRAFT — design only.** No implementation. Blocked on operator sign-off for **D1**. |
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

### D1 — Who answers *(OPERATOR SIGN-OFF REQUIRED — do not implement past this line)*

Today's ladder, already shipped by SPEC-055 and live:

| Message shape | Who wakes | Cost |
|---|---|---|
| `priority == critical` | every member | N full runs |
| `@mentions` present | only the named agents (+ guaranteed inbox fanout) | M full runs, M = names |
| no mentions (broadcast) | every member runs `quick_classify`; those answering YES run | N × 8-token calls + K full runs |
| DM | the addressee | 1 full run |

The ladder is sound. The mention tier is right and I am **not** proposing to
change it — the brief asks whether "hints, not routing" was a deliberate decision
being overridden. It was a decision, it was already reversed by SPEC-055 on
recorded live evidence (one `@josh_agent` post to `#brand` caused four agents to
burn a full Sonnet turn each), and that reversal was correct. The stale docstring
should be deleted, not honoured.

**The open question is only the un-addressed tier.** Three options:

| Option | Un-addressed post costs | Failure mode | Determinism |
|---|---|---|---|
| **A — triage (today)** | N cheap classify + K full runs | fail-open ⇒ **everyone answers** | model judgement |
| **B — channel responder** | 1 full run | wrong agent answers; cheap and visible | operator config |
| **C — silence unless addressed** | 0 | human must always `@` someone | absolute |

**Recommendation: B as the per-channel default, A available as an opt-in, C never.**

Reasoning, in the order that decided it:

1. **Triage's failure mode is the thing triage exists to prevent.** It is
   fail-open by design and correctly so — `_passes_channel_triage` wakes the run
   on a missing classifier, an exception, or a garbled verdict
   (`capabilities.py:166-184`). A broken or slow model therefore degrades to *N
   full runs on every channel message*, which is the exact ~15× fan-out
   anti-pattern. A cost control whose failure is "spend the maximum" is the wrong
   shape for a cost control.
2. **A designated responder's failure is cheap and legible.** One agent answers
   something outside its lane. The operator sees it and re-points the field. No
   token cliff, no six-way pile-on.
3. **It matches the Slack mental model the owner asked for.** Channels have
   owners. `#work` having a responder is not an Arc concept, it is how the room
   already works socially.
4. **It moves a cost-bearing product decision from a model to the operator**,
   per channel, which is where the brief says it belongs.

Shape: `Channel.responder: str = ""` (a DID) on `arcteam.types.Channel`. Empty
means fall back to the channel's `triage` setting. Set via the existing
operator-only channel-management routes — no new surface. `arcagent` reads it
from the channel record it can already fetch via `list_channels()`.

**This is the decision to bring to the owner.** Everything below is settled.

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

**The ceiling, stated plainly for an N-member channel:**

| Case | Classify calls | Full runs |
|---|---|---|
| Human `@mentions` M agents | 0 | M |
| Human un-addressed, **option B** | 0 | 1 |
| Human un-addressed, **option A** | N (8 output tokens each) | K ≤ N |
| Human un-addressed, **option A, classifier broken** | N (failed) | **N** |
| Agent reply, with D4(a) | 0 | 0 |
| Agent reply, **without D4(a)** | N | unbounded |

`quick_classify` is genuinely cheap and correctly built for this — one `arcllm`
call, no tools, `max_tokens=8`, explicitly *not* the agentic loop
(`agent.py:732-749`). The expensive row is the fourth, which is D1's argument.

Controls to engage, all existing:

- **`RootTokenBudget`** (`arcagent/orchestration/token_budget.py`) — a channel
  post opens one budget shared by every activation it causes. `try_debit` is
  atomic and returns `False` rather than overspending, so exhaustion **refuses
  further activation** instead of truncating a turn mid-thought. This is the
  primitive that already exists to fix the "children silently spend multiples of
  the caller's allocation" bug; a channel fan-out is that bug's shape.
- **`CircuitBreaker`** — per D4(d).
- **Triage stays fail-open, but a triage failure is now audited and counts toward
  the breaker.** Correctness wins the individual message; the breaker stops a
  broken classifier from buying N full runs on every message forever. Today a
  failing classifier degrades silently and permanently.

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

## Open questions for the owner

1. **D1 — un-addressed posts.** Recommendation is B (per-channel responder,
   triage opt-in). This is the one with real cost consequences and it is not
   settled.
2. **Auto-create on first post.** F1 says the current behaviour creates a
   one-member room. Options: stop auto-creating and require the channel to exist;
   or auto-create seeded with the full roster. Auto-creating an empty room is the
   one thing that must not survive.
3. **Should `#work` membership default to every registered agent?** Convenient
   and matches "the team channel"; also the largest fan-out surface.

---

## Also found (out of scope, flagged not fixed)

- **`classification` is not in `crypto._SIGNED_FIELDS`** (`crypto.py:24-38`).
  It is enforced on send (`_enforce_no_write_down`) but not covered by the
  signature, so it is not tamper-evident in transit the way `body` and `mentions`
  are. Not this spec's problem; worth its own look given SPEC-038.
- **`team_chat.py`'s module docstring** describes the file as read-only while the
  same file exposes three operator-only mutation routes. It sent this
  investigation down the wrong path and should be rewritten regardless of what
  happens to this spec.
