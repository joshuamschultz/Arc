# SDD — SPEC-032 ArcStore Team Directory + arcui Integration

## Module boundaries (hard contracts)

| Module | Owns | Contract | MUST NOT |
|--------|------|----------|----------|
| **arcstore** | Two planes: immutable telemetry (exists) + **new mutable keyed store** | `record()`/`read()` (immutable, unchanged); new `MutableStore` (get/put/delete/list) | import arcteam/arcagent/arcui (stays a leaf) |
| **arcteam** | Directory model + composite backend | `ArcStoreDirectoryBackend`, `CompositeBackend`; `EntityRegistry`/`Team`/`Channel` unchanged | own message transport; import arcagent/arcui |
| **arcagent** | Daemon: dynamic channel re-subscribe | inbox loop watches membership → re-subscribe | define directory schema; own UI |
| **arccli** | `arc team channel …`; build MessagingService for `arc ui start` | thin commands over arcteam | hold directory logic |
| **arcui** | View + forward (unchanged role) | consume `messaging_service`, roster from directory, `team_post_forwarder` | write directory logic; sign |

**Dependency edge added:** `arcteam → arcstore` (new, non-cyclic — arcstore never imports arcteam). `arcstore` stays a leaf under `arctrust`.

---

## Current state (from investigation — file:line)

- **arcstore** SQLite, WAL, **insert-once** — only `INSERT OR IGNORE`, no update/delete (`packages/arcstore/src/arcstore/backends/sqlite.py:166-174,240`); fixed 5-table telemetry schema (`backends/base.py:18-35`); the one mutable pattern is `sync_state` `ON CONFLICT DO UPDATE` (`sqlite.py:294-298`). API: `record()`/`read()`/`open_backend()` (`__init__.py:27-34`).
- **arcteam `StorageBackend`** is directory-half (`read/write/delete/query/list_keys/exists`, `storage.py:41-100`) + stream-half (`append_auto_seq/read_stream/open_consumer`, `storage.py:53-107`). Only `NatsBackend` + `MemoryBackend` exist; **no arcstore backend** (`backends/` = `nats.py` only).
- **arcui roster** = disk scan `arcgateway.team_roster.list_team` over `team_root/*_agent/arcagent.toml` (`team_roster.py:49-116`), not the arcteam registry; `agent_registry` is a liveness overlay only (`server.py:356-365`).
- **arcui channels** empty because `arc ui start` calls `create_app(...)` with **no `messaging_service`/`team_post_forwarder`** (`arccli/.../commands/ui.py:222-227`); `create_app` already supports them + spins up `TeamBusObserver` (`arcui/server.py:121-122,271-280,336`). Frontend switcher+composer already exist (`arcui/web/src/pages/messages.tsx`, `hooks/use-team-stream.ts`).
- **Reference** to build a live service: `arccli/.../commands/team.py:_build_service` (`:113-135`).

---

## Component design

### C1 · arcstore mutable plane — *arcstore* `mutable.py` (REQ-001..003)
- New `MutableStore` over a new SQLite table `mutable_records(collection TEXT, key TEXT, value JSON, updated_at TEXT, PRIMARY KEY(collection,key))`. Writes use `INSERT … ON CONFLICT(collection,key) DO UPDATE` (the proven `sync_state` idiom); `delete(collection,key)`; `get`; `list(collection, filters=)`.
- Kept **separate** from the immutable telemetry backend (`backends/base.py` Protocol) — a distinct module + table; do not overload the insert-once trace mirror. Exposed via `arcstore.__init__`.
- **Pillar:** Modularity (two planes, one concern each) + Scalability (WAL, shared-nothing).

### C2 · arcteam directory backend — *arcteam* `backends/arcstore.py` (REQ-010)
- `ArcStoreDirectoryBackend` implements the **record-half** of `arcteam.storage.StorageBackend` (`read/write/delete/query(filters,prefix)/list_keys/exists`) over `arcstore.MutableStore` (collection = arcteam collection name, key = arcteam key). Stream-half methods raise `NotImplementedError` (never called on this backend — see C3).
- **Pillar:** Modularity — arcteam owns the mapping; arcstore owns storage.

### C3 · Composite backend — *arcteam* `backends/composite.py` (REQ-011)
- `CompositeBackend(directory: StorageBackend, streams: StorageBackend)` implements the full Protocol: record-ops → `directory` (arcstore), stream-ops (`append_auto_seq/read_stream/read_last/get_stream_end_byte_pos/open_consumer`) → `streams` (NATS). `MessagingService(backend=composite, …)` unchanged.
- Retire the NATS KV path for entities/teams/channels (REQ-012): those `read/write/query` now hit the directory. Delete the dual path in the same edit.
- **Pillar:** Modularity + Simplicity (single source of truth).

### C4 · arcui wiring — *arccli* `commands/ui.py`, *arcui* `server.py` (REQ-020..022)
- `arc ui start`: build `backend = CompositeBackend(ArcStoreDirectoryBackend(resolve_data_dir()), NatsBackend.connect(url))`, then `AuditLogger` + `EntityRegistry` + `MessagingService(signer=<ui/human signer>)` (mirror `team.py:_build_service`); pass `messaging_service=svc` **and** `team_post_forwarder=<forwarder>` to `create_app`. No arcui code change needed for channels/live stream — the params + lifespan observer already exist.
- **Roster source (REQ-021):** replace/augment `_roster_provider` (`server.py:356-365`) to read `EntityRegistry.list_entities()` (agent entities) from the directory, merged with `agent_registry` liveness for `online`. Keep the disk-scan as a fallback/enrichment for on-disk-only agents.
- **Pillar:** Modularity — arcui stays view+forward; all writes go through arcteam.

### C5 · Channel membership control — *arccli* + *arcui* + *arcagent* (REQ-030..033)
- **CLI:** `arc team channel create|list|join|leave` over `MessagingService.create_channel/join_channel/leave_channel/list_channels` (already exist, `messenger.py:557-604`), persisted in the directory.
- **arcui:** operator create-channel + add/remove membership → calls arcteam (via an authenticated `/api/team/channel/*` route, operator-token gated). arcui does not mutate the directory directly.
- **Dynamic re-subscribe (REQ-032):** the arcagent inbox loop watches its channel membership (poll the directory or subscribe to a `directory-changed` signal) and, on change, tears down/rebuilds its `subscribe()` set — no daemon restart. `subscribe()` currently reads membership once at start (`messenger.py:397-401`); add a re-subscribe trigger.
- **Pillar:** Simplicity + Scalability.

### C6 · Human interaction — *arcteam*/*arccli* + *arcui* (REQ-040..042)
- **Persisted human key (REQ-040):** `arc team register --type user` mints AND persists the human's Ed25519 identity to a stable location the send path resolves (fix `_signer_for` to find a user's key at `root/keys`, not only a workspace). `MessageSigner.from_identity` over that key.
- **Forwarder (REQ-041):** `team_post_forwarder(sender, channel_or_dm, text)` builds a signed `Message` as the human entity and calls `svc.send`. Wired in `arc ui start`. arcui composer already emits `{"type":"post",…}` to `/ws/team` (`team_ws.py`).
- **Pillar:** Security (signed human posts) + Modularity.

---

## Data model (arcstore mutable plane)

```
mutable_records(collection, key, value JSON, updated_at)  PK(collection,key)   -- ON CONFLICT DO UPDATE
# tenants (SPEC-032): collection ∈ {messages/registry, messages/channels, teams}
# future tenants: agent-configs, settings — same table, new collections
```
arcteam records (Entity/Team/Channel — `types.py:131-171`) serialize to `value`; keys are arcteam's existing `_entity_key`/team id/channel name.

## Security posture
Directory writes audited (REQ-003); membership is access-control state, now durable+auditable; human posts Ed25519-signed (REQ-040/041); arcui `/api/*` stays viewer/operator token-gated (`auth.py:241-247`); arcui never signs or writes the directory (REQ-022).

## Scalability posture
SQLite WAL shared-nothing directory (point reads, cheap); NATS for message fan-out; roster read-on-demand (mirrors `Observe`); dynamic re-subscribe is edge-triggered, not a polling storm.

---

## Research Insights (from /deepen)

Five parallel research streams (SQLite dual-plane · NATS subscription churn · directory-of-record vs bus · roster+live UI · human-key signing). Load-bearing findings + the design refinements they drove. Full citations in the deepen transcripts.

### RI-C1 · arcstore mutable plane (SQLite)
- **Two planes = two tables** in one file: insert-only telemetry (enforce with `BEFORE UPDATE/DELETE → RAISE(ABORT)` triggers) + mutable `directory` (upsert). One file gives **atomic "mutate + audit" commits**; split to two files only if telemetry checkpoint pressure starves the directory's WAL (SQLite WAL couples checkpoint health across all tables in a file).
- **Mutate-in-place + a trigger-fed companion mutation log** (`old_json/new_json/changed_by_did/changed_at`, same transaction) — NOT full event-sourcing (a cited production anti-pattern; membership is an O(1) authz hot-path read, so replay-to-derive-state adds an ASI08 lag risk). Soft-delete (`deleted_at`), `updated_at` via `AFTER UPDATE` trigger.
- **Concurrency:** WAL + `busy_timeout` 5–60s + `BEGIN IMMEDIATE` for writers. SQLite is single-writer; with CLI + UI + daemons on one file, the hardened pattern is a **single elected write-owner** (others proxy) — `busy_timeout` arbitration among many processes is itself a DoS surface. *Decision: `busy_timeout` is fine at current scale; document write-owner as the scaling/hardening path, don't build it now.*
- **Migrations:** `PRAGMA user_version`-gated, atomic (`BEGIN; DDL; PRAGMA user_version=N; COMMIT`), run on open by every process → self-healing across mixed code versions.

### RI-C3 · Directory-of-record + migration off NATS-KV
- **Control-plane/data-plane** is the exact prior art (Consul *catalog* vs *KV*, Tailscale, k8s): directory = authoritative queryable store; bus carries only already-authorized traffic. The pain SPEC-031 hit (no relational/group query in bus-KV) is *why* KV is the wrong home.
- **Cutover:** shadow-write → verify → **atomic cutover, then revoke write capability to the old NATS-KV path** (provable, not "we stopped calling it") + emit an arctrust audit marker. **No permanent dual-write**; delete migration code in the same change (no shims).
- **Directory = authz store** → NIST 800-53 **AC-2/AU-2**: every membership mutation is a `caller_did`-attributed, `PolicyPipeline`-checked, `arctrust.audit.emit`'d event — one write path, not a bare DB write.

### RI-C5 · Dynamic re-subscribe (the biggest refinement)
- **Desired state (arcstore) vs observed state (NATS subs) → reconciliation loop** (k8s controller pattern): **event-driven for latency + periodic reconcile for correctness** — a stale subscription is a *live authz bypass*, so reconcile is a security control, itself audited.
- **Signal via NATS KV-watch, not polling:** arcteam maintains a **derived NATS-KV mirror** of each agent's membership (SQLite stays source of truth); the daemon **watches** it (push + current-value replay on reconnect = self-healing). Agents **never write** their own membership.
- **Per-channel durable consumer** named `agent-{did}-{channel}`; one `asyncio.Task` per channel wrapping a `fetch()` loop (not callback `consume()`); `dict[channel]→task` registry with a **client-side dedup lock** (the server-side already-bound check races, nats.go #4607); **delete the durable on "leave"** (unsubscribe alone leaves server-side ack state + stuck messages); never delete+recreate to "refresh" (double-delivery) — re-bind by name.
- **Grants lazy, revocations eager** (bounded eventual consistency; the DB already gives strong consistency at commit — only propagation lags). Durable (not ephemeral) — agent is a persistent service. Capacity-plan agent×channel durables (Raft-replicated, "not free"); if it grows, consolidate to one durable-per-agent with an arcteam-updated subject-filter set.
- *PLAN impact: D3 becomes "KV-watch + local-diff reconciliation loop + per-channel durable lifecycle," and membership writes (D1/D2) go through `PolicyPipeline` on the requester's DID.*

### RI-C4 · arcui roster + live feed
- **Roster = durable directory ⊕ ephemeral presence overlay, merged at view time** (Slack Flannel / Pusher / Ably) — presence is NEVER written into the directory; use **heartbeat + grace window** (~15s) so a tab blip isn't a "leave" flap. This is exactly the `agent_registry` liveness overlay (C4) — keep it read-side only.
- **One bus consumer → many viewers** (the existing `TeamBusObserver`/`TeamStreamHub` is already this shape): bounded per-viewer buffer + **drop-oldest** + **sequence replay for late joiners** — already implemented in `team_stream.py`; the research validates it.
- **REST history + WS live delta; no interval poll** (confirms removing the 5s poll). **Operator vs viewer authz: server-side scope check on every mutating call** (OWASP API5) — hiding a button is cosmetic. Token scopes `channel:create`/`membership:write`/`post:as-human`, validated per-call via `PolicyPipeline`.

### RI-C6 · Human interaction — **design change (do not UI-hold the human key)**
The naive "UI server signs with the human's raw key" is the **highest-risk option and breaks non-repudiation** (sole-possession → sender==signer). Revised C6:
- **CLI human-send = local signing (safe, primary path):** the human's key lives in their own store; `arc team send` signs locally. Fix `_signer_for` into a **`Signer` Protocol resolved by identity *kind*** (not "has a workspace") with a human backend (reuse arctrust's existing 0600/0700 file enforcement; keychain/vault/hardware as backends); **fail-closed with a precise error** if the key is missing — never silent-unsigned.
- **arcui composer = delegated forwarder, NOT key-holder:** the forwarder gets its **own DID** and signs `signer=ui-forwarder, on_behalf_of=human` (RFC 8693 `act`-claim style), separately `PolicyPipeline`-authorized and audited; OR client-side signing (browser passkey / local CLI) with the server only relaying an already-signed envelope. Backend derives the caller from the validated token, **never a client "from"**; audit **both** the authenticated caller and the attributed display identity. Optimistic UI via a client correlation-id reconciled on the confirmed feed event.
- Reuse the existing `canonical_bytes`/`_verify_origin` (sender==signer) — extend the registry to carry human DIDs; verify-only identities already first-class.

*These insights update REQ-032 (reconciliation + KV-watch), REQ-041 (delegated forwarder, not UI-key), and add a "single-write-owner (deferred)" note to REQ-002. Original requirements/components above stand; this section refines the how.*
