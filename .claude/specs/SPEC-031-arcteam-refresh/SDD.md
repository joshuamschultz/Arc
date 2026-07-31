# SDD — SPEC-031 ArcTeam Refresh

## Module boundaries (Modularity pillar — hard contracts)

| Module | Owns | Contract exposed | MUST NOT |
|--------|------|------------------|----------|
| **arctrust** | DID, Ed25519 keypair, policy pipeline, audit chain | `AgentIdentity`, `keypair.sign/verify`, `PolicyPipeline.evaluate`, `audit.emit` | know about teams/messages |
| **arcrun** | Loop execution + steering queue | `RunHandle.steer(caller_did, msg)`, `.follow_up(caller_did, msg)`, `.cancel()`; emits `steer.injected`/`followup.injected` | make trust decisions; call LLM; know agent/team semantics |
| **arcteam** | Coordination of many entities: registry, team, roster, messenger, transport, mentions, message signing | `EntityRegistry`, `Team`, `Roster`, `MessagingService`, `resolve()`, `NatsBackend` | run an agent loop; call LLM; own UI |
| **arcagent** | One agent: identity source, inbox loop, steering caller | consumes `arcteam` messenger; calls `arcrun` steering **after** policy gate | define a team; own transport |
| **arcui** | View + input forward | read-only bus subscriber; forwards human input to arcagent (1:1) or arcteam (group) | route, sign, coordinate, run loops |
| **arccli** | `arc team` / `arc agent` commands | thin command layer over arcteam/arcagent | hold business logic |
| **arcgateway** | External platform seam (deferred) | `gateway → arcteam` adapter contract (design only) | — |

**Rule:** no PLAN task crosses a boundary. Cross-module interaction is via the contracts above only.

---

## Current state (from investigation — file:line)

- **arcteam has no DID** — `Entity.id` is a bare URI (`registry.py:15-17`); zero `arctrust`/DID usage. Two registration paths conflict: `arc agent create` sets `id=name` (`arccli/.../agent/create.py:86-92`) vs messenger expecting `agent://name` → `sender_unauthorized` DLQ.
- **No Team model** — `team.py/roster.py/coordinator.py/consensus.py` absent; teams = entities+channels. (`arcgateway/.../team_roster.py` exists separately.)
- **Transport** — `FileBackend`/`MemoryBackend` behind `StorageBackend` Protocol (`storage.py:27`); NATS is naming-only (no `nats` import).
- **No message signing** — `send` path has no sig/nonce (`messenger.py:112-194`); only HMAC audit chain (`audit.py`).
- **Steering exists** — `RunHandle.steer/follow_up/cancel` (`arcrun/loop.py:193-210`), drained in `react.py:116,136,186,234`; injected as `user` role (`_messages.py:25`); **unused**, and missing `caller_did` + audit emit + policy.
- **Autonomy exists but off** — `modules/messaging/__init__.py:353` `_poll_loop`; enabled only if `[modules.messaging]` set (default config omits it, `_common.py:86`). `arc agent serve` is a real daemon (`serve.py:18-51`). `arcmas` is an empty stub.
- **arcui push is dead** — `emit_team_event` fires with no `UIReporter` in source; team UI polls DB every 5s (`web/src/lib/queries.ts:81`); `test_no_push_pipeline.py` bans the old parallel push modules. Live `/ws/chat` (1:1) works via `arcgateway/adapters/web.py`.

---

## Component design

### C1 · Identity & resolution — *arcteam* (REQ-001..003)
- `Entity` gains required `did` + `handle`; storage keyed by DID. Registration pulls the real DID from `arcagent.core.agent.AgentIdentity` (already minted, `agent.py:138`) — arcteam stops inventing ids.
- `resolve(ref) -> DID` single path for `@handle | agent:// | user:// | channel:// | role://`. Unknown handle → `UnknownHandle` (typed).
- **Pillar:** Security (DID fail-closed) + Simplicity (one resolver).

### C2 · Mentions — *arcteam* `mentions.py` (REQ-004)
- Pure `extract_mentions(body) -> list[DID]`; on `send`, set `Message.mentions` + attention flags. No state. **Pillar:** Simplicity.

### C3 · Team & Roster — *arcteam* `team.py`, `roster.py` (REQ-010..011)
- `Team(id, name, members: list[DID], default_channel, goal_ref: str|None, created)`; `TeamStore` on `StorageBackend`. `Roster.snapshot()` joins registry + presence. Reconcile the gateway `team_roster` into this one concept. No consensus/coordinator (YAGNI). **Pillar:** Modularity.

### C4 · Presence — *arcteam* `registry.py` (REQ-021 routing)
- `Entity.status: active|idle|blocked|waiting|offline`; set on serve start/stop + turn start/end. Router: push to `active`, durable-consumer inbox covers `offline`. **Pillar:** Scalability.

### C5 · NATS transport — *arcteam* `backends/nats.py` (REQ-020..022)
- `NatsBackend` implements `StorageBackend` → drop-in; messenger API unchanged. JetStream stream per subject family; **durable consumer per entity = push + resume-from-ack** (this is the push-first + inbox primitive AND the UI observer source). JetStream ack floor replaces hand-rolled cursors. Connection pool, reconnect w/ backoff, bounded in-flight. `nats-server` dev dependency. **Pillar:** Scalability + Modularity.

### C6 · Signed envelope — *arcteam* `types.py`, `crypto.py` (REQ-030..031)
- `Message` gains `sig`, `nonce`, `signer_did` (has `ts`). Sign on `send`, verify on consume via `arctrust` keypair. Replay cache keyed by `nonce` (TTL window). HMAC audit chain stays (audits ops; sig authenticates messages). **Pillar:** Security.

### C7 · Steering hardening — *arcrun* `loop.py`, `strategies/react.py` (REQ-042)
- `steer`/`follow_up` require `caller_did`; `bus.emit("steer.injected"/"followup.injected", {caller_did,…})` at each drain (`react.py:116,136,186,234`). arcrun stays a dumb **identified** queue — no policy/LLM here. **Pillar:** Security + Modularity.

### C8 · Steering adoption — *arcagent* inbox handler (REQ-040..041)
- Inbox handler runs incoming message through `arctrust` policy, then calls `follow_up` (default) or `steer` (critical + allow). The **only** new caller of steering. Trust decision lives here, not in arcrun. **Pillar:** Modularity + Security.

### C9 · Autonomy + boot — *arcagent* config, *arccli* `arc team up/down` (REQ-050..051)
- Team-member scaffold enables the inbox loop by default. `arc team up/down` supervises N `arc agent serve` daemons on the team's subjects, mirroring the `arcgateway/runner.py` TaskGroup pattern (reuse, don't reinvent). Fill the empty `arcmas` or place in `arccli/commands/team.py`. **Pillar:** Scalability + Simplicity.

### C10 · UI view — *arcui* `routes/*`, `web/` (REQ-060..063)
- New read-only WS: subscribe to team/channel subjects, fan out to browser (generalize `arcgateway/adapters/web.py` chat_id fan-out to channel scope). Render handles + mentions.
- Human input **forwarded**: group→arcteam (signs/routes as that human), direct→arcagent. arcui never signs/routes.
- Remove dead `emit_team_event→UIReporter` stub, 5s poll, and leftover messaging patch. Re-scope `test_no_push_pipeline.py`: forbid *parallel* push, allow single-bus read-only subscriber. **Pillar:** Modularity + Simplicity.

---

## Data model deltas

```
Entity   += did: str (req), handle: str (req, unique/team), status: Enum
Message  += sig: str, nonce: str, signer_did: str, mentions: list[str]
Team      = {id, name, members: list[did], default_channel, goal_ref?, created}
```

## Security posture (pillar 3)

DID on every entity (Identity) · Ed25519 signed messages + replay window (Sign) · policy-gated steering + first-DENY-wins (Authorize) · audit on every op + injection (Audit). Steered content stays `user`-role data (mitigates LLM01/ASI06); signed inter-agent messages (mitigates ASI07). Federal tier locks steering + requires signed allowlist via **config**, not code branches.

## Scalability posture (pillar 4)

Shared-nothing per agent; NATS bus (no singleton); durable consumers; supervised reconnecting daemons; target ≥10k msg/min across 10–20 agents. `NatsBackend` swap-in keeps horizontal scale a config choice.
