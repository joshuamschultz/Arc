# PRD — SPEC-032 ArcStore Team Directory + arcui Integration

**Format:** EARS. Each requirement carries a governing **pillar** and a pillar-tied acceptance criterion. IDs are monotonic `REQ-NNN`. MoSCoW priority.

---

## Goal & non-goals

**Goal:** a durable, mutable team directory in arcstore that arcui and the CLI read/write, so the UI shows the team live and a human can manage channels and chat with agents.

**Non-goals (this spec):** Slack mirroring (SPEC-033); migrating agent configs/settings into the mutable plane (future tenants — the plane is designed to accept them, but SPEC-032 only moves the team directory).

---

## Requirements

### arcstore mutable plane — *Must*

- **REQ-001** *(Modularity)* arcstore SHALL provide a **mutable, keyed record store** distinct from the immutable telemetry backend, supporting create, **update (overwrite)**, **delete**, get, and filtered list by collection.
  - *Accept:* the immutable telemetry `StorageBackend` (`INSERT OR IGNORE`) is untouched; the mutable store lives in its own module with its own table(s).
- **REQ-002** *(Scalability)* The mutable store SHALL use SQLite WAL with `ON CONFLICT DO UPDATE` semantics (the `sync_state` pattern), shared-nothing, one DB under `resolve_data_dir()`.
  - *Accept:* a repeated write to the same key **overwrites** (not a no-op); a delete removes the row; concurrent readers don't block writers.
- **REQ-003** *(Security)* Every mutable-store write and delete SHALL be attributable and auditable.
  - *Accept:* directory mutations (entity/team/channel/membership) emit an audit event.

### arcteam directory backend — *Must*

- **REQ-010** *(Modularity)* An `ArcStoreDirectoryBackend` SHALL implement the **record-half** of arcteam's `StorageBackend` Protocol (`read/write/delete/query/list_keys/exists`) over the arcstore mutable plane.
  - *Accept:* `EntityRegistry`, `Team`, and channel storage run unchanged on it (they depend only on the Protocol).
- **REQ-011** *(Modularity)* A **composite backend** SHALL route directory record-ops to `ArcStoreDirectoryBackend` and message stream-ops (`append_auto_seq/read_stream/open_consumer`) to `NatsBackend`, satisfying the full `StorageBackend` Protocol.
  - *Accept:* `MessagingService` is constructed over the composite with **no messenger code change**; directory survives a NATS restart; messages still flow over NATS.
- **REQ-012** *(Simplicity)* The directory SHALL be the single source of truth for entities/teams/channels; the NATS KV registry SPEC-031 used for these SHALL be retired.
  - *Accept:* no entity/team/channel record is written to NATS KV; `grep` finds no dual-write.

### arcui integration — *Must*

- **REQ-020** *(Modularity)* `arc ui start` SHALL construct a `MessagingService` (over the composite backend) and pass it as `messaging_service` to `create_app`, and SHALL point the directory at the same `resolve_data_dir()` arcstore `Observe` uses.
  - *Accept:* `/api/team/channels` returns registered channels; `/ws/team` streams live bus frames via the existing `TeamBusObserver`.
- **REQ-021** *(Simplicity)* The arcui roster SHALL be fed from the **arcteam directory** (`EntityRegistry.list_entities`) merged with the existing liveness overlay (`agent_registry`), not only the `team_root` disk scan.
  - *Accept:* an agent registered in the directory but with no on-disk workspace still appears in `/api/team/roster` and the Messages sidebar.
- **REQ-022** *(Modularity)* arcui SHALL remain view + forward only (SPEC-031 REQ-060): it renders the directory and forwards human input; it does not own directory logic.
  - *Accept:* the boundary test still passes; arcui contains no directory write logic beyond calling arcteam.

### Channel membership control — *Must*

- **REQ-030** *(Simplicity)* The `arc team` CLI SHALL expose `channel create`, `channel join <agent> <channel>`, `channel leave <agent> <channel>`, and `channel list`, persisted in the directory.
  - *Accept:* a joined agent's membership is durable and visible to arcui and other processes.
- **REQ-031** *(Modularity)* arcui SHALL let an operator create channels and add/remove an agent's membership (writes go through arcteam, not arcui).
  - *Accept:* toggling membership in the UI updates the directory and the agent's monitored set.
- **REQ-032** *(Scalability)* A **running** agent daemon SHALL re-subscribe when its channel membership changes, without a restart.
  - *Accept:* adding a running agent to a channel causes it to receive that channel's next message; leaving stops delivery — no restart.
- **REQ-033** *(Simplicity)* An agent MAY monitor a subset of channels (e.g. 3 of 10); it receives only channels it is a member of.
  - *Accept:* a message on a non-member channel is not delivered to that agent.

### Human interaction — *Must*

- **REQ-040** *(Security)* A human SHALL be a first-class entity with a **persisted Ed25519 key** locatable by the send path, so human messages are signed.
  - *Accept:* the `_signer_for` "no workspace" gap is fixed; a human can `arc team send` and it verifies.
- **REQ-041** *(Modularity)* `arc ui start` SHALL wire a `team_post_forwarder` that builds a **signed** `Message` as the human entity and calls `svc.send`, so the existing arcui composer posts to channels and DMs.
  - *Accept:* posting from the Messages composer reaches the target channel/DM and agents respond; without it the composer returns `forward_unavailable`.
- **REQ-042** *(Simplicity)* A human SHALL interact via both **channels** (group) and **DMs** (1:1) from arcui and CLI.
  - *Accept:* a human DM to `@agent` and a post to `channel://x` both deliver and render with handles.

### Cleanup — *Should*

- **REQ-050** *(Simplicity)* Superseded paths SHALL be removed: the NATS KV registry for entities/teams/channels; the disk-scan-only roster where the directory now supplies it.
  - *Accept:* no dual source of truth; `ruff`/`mypy --strict` clean; no compat shims.

---

## MoSCoW

- **Must:** REQ-001…042 (mutable plane, directory backend, arcui wiring, channel control, human interaction).
- **Should:** REQ-050 (cleanup, inline with the edits that supersede each item).
- **Won't (this spec):** Slack mirror (SPEC-033); agent-config/settings migration into the mutable plane.

## Traceability

Every REQ → SDD component → PLAN task; tasks scoped to one module.
