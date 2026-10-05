# Connected-data writes in their own process (the sync worker)

## Why

One `arc ui start` process hosts the dashboard, the gateway, every agent and,
until now, every connected-data sync. On the DGX (2026-10-03) the syncs caused
on-loop SQLite commits, D-state disk waits on the loop thread and GIL contention
from extraction and chunking threads. The result was chat lag, NATS request
timeouts and 0.2-4 s health-check spikes.

The heavy work is not the sync's bookkeeping. It is the store write: extraction,
sanitize, chunking, embedding, FTS and vector writes, and the OKF index and seal
rebuilds. All of it sits behind one seam, `IngestPort`. So that seam is where
the process boundary goes.

## The shape

```
main process (arc ui start)                          sync worker (child)
---------------------------                          -------------------
ConnectedDataService (schedule, leases, cursors)
SyncCoordinator (provider fetch, page commits)
RemoteIngestPort  ── worker.sock: every store write ──▶ StoreWriters
  reads: read-only SQLite (mode=ro, query_only)          ArcMemoryIngestAdapter (writes)
HostService       ◀── host.sock: sign a seal, ─────────  RemoteSealSigner
                       arcstore rows, audit              HostObjectState / HostApprovals
```

* **The worker owns every document-store write.** That covers `ingest`,
  `complete_snapshot`, `finish_sync`, `refresh_operator_guide`, `relayout_source`,
  `reset_source`, `purge_source`, `adopt_documents` (migration), the shared
  store's embedding-profile claim and its deletion, and the doc-embed backfill
  (vectors). This applies to agents' own stores and to shared connection stores
  alike.
* **The main process only reads.** `RemoteIngestPort` implements the same
  `IngestPort` contract as `ArcMemoryIngestAdapter`, so the coordinator and
  `ConnectedDataService` do not change. Its reads (search, listing, counts, the
  routing overview) run against `MemoryDB(read_only=True)`: the file is opened
  `mode=ro` with `query_only`, a store that does not exist yet reads as empty,
  and a reader's vector sidecar is built in memory and never saved. Every write
  entry point of a read-only handle raises before it touches anything.
* **What stays in the main process.** Scheduling, leases, cursors, provider
  fetch and credentials all stay here. They are async I/O plus small arcstore
  rows. The agent's own memory module (episodic, semantic) also keeps writing
  its own state here (ADR-029). Only connected-data stores moved.

## Process model

* `arc ui start` spawns the worker in its lifespan, before any agent, and stops
  it after them. Any other process that hosts an agent starts it when its
  connected-data module starts. It is never a second systemd unit, and it needs
  no configuration.
* The child is `python -m arcagent.modules.connected_data.sync_worker`, run from
  the same interpreter, so it uses the same runtime venv. `arc sync-worker` is
  the same entry point. It refuses to start without a bootstrap on stdin.
* **No orphans.** The child exits when its stdin reaches EOF, when its parent pid
  changes, or, on Linux, when it receives the parent-death signal. On `stop`,
  the parent closes stdin, sends SIGTERM and then SIGKILL after a grace period.
* **Restart.** Capped exponential backoff with jitter (0.5 s doubling to 30 s).
  The backoff resets after the worker has been up for 60 s.
* **Warm before the first write.** The worker is a fresh process, so its first
  embed would load the embedding model (torch and weights, seconds) inside a
  sync run's first page, where the stall guard would count it as a hung
  provider. An agent's start asks the worker to load that agent's local model
  (`warm`, bounded by the start's 15 s wait). The supervisor remembers each
  agent's settings and warms every restarted worker before it reports `up`.
  A remote (`provider`) embedder is never called to warm.
* **Watchdog.** A ping every 5 s. Four missed pings in a row mean the worker is
  hung: it is killed and restarted.

## Control channel and authentication

Both sockets live in a private 0700 directory, and each socket file is 0600.
The frame is `>II` (header length, body length), then a JSON header, then raw
body bytes. Document bytes never go through base64.

* **Secret.** A new 32-byte secret is made for every spawn. It is written to the
  child's stdin, never to argv or the environment.
* **Every frame** carries an HMAC-SHA256 over the canonical header (its direction
  included) and the SHA-256 of the body. It also carries a timestamp, which must
  be within 30 s, and a nonce, which may be used only once. A request cannot be
  replayed, reflected as a reply, or carried to the other channel.
* **Peer credentials** are checked before the first byte is read
  (`SO_PEERCRED` / `LOCAL_PEERCRED` + `LOCAL_PEERPID`). The peer must have the
  same uid and, where the platform reports it, the expected pid. The worker
  accepts only its parent. The parent's host socket accepts only the current
  child.
* **A failed check is refused silently.** The connection closes with no reply,
  the refusal is logged, and an audit event is sent through the host channel.

The protocol is the same at every tier. Federal adds nothing to relax, and an
unauthenticated frame is refused everywhere.

## Identity, authorization and keys

The worker holds **no provider credential, no database credential and no
private key**, and it makes no provider calls. Whatever else a write needs, it
asks the main process for over the `host` channel:

* **Seal signatures.** The worker's `RemoteSealSigner` sends each seal payload to
  the main process. `arcmemory.okf_seal.sign_for` signs only a canonical seal
  payload for a collection inside that store, under exactly the pinned identity:
  the agent's own key for its workspace, or the connection's knowledge principal
  `did:arc:knowledge:<hash>` for a shared store. A request for arbitrary bytes is
  refused, so the capability cannot become a confused deputy.
* **Arcstore rows.** Per-object versions, the source incarnation, mapping
  approvals and knowledge subscriptions. The main process checks each request:
  an object-state owner must be an agent it serves or a knowledge principal,
  approval rows are listed or created only for such an agent, and a created row
  must be a mapping proposal.
* **Stores the worker will write.** The worker derives every root itself and
  refuses a request whose root disagrees:
  * An agent's own store is the workspace that the agent's own `arcagent.toml`
    names. That config must declare the requesting DID.
  * A shared store is `connected_knowledge_dir() / store_key(connection_id)`.
* **Who may write a shared store.** Each authority is checked by the worker
  before every write:
  * `subscriber`: the writer's live subscription, asked again before every write.
  * `migration`: the agent's own approved document mapping. It allows only
    adopt and claim.
  * `orphan`: allows only purge and drop, and only once no subscription is left.

## Failure behavior

* **Worker down or restarting.** A write fails fast with
  `SyncWorkerUnavailableError` (`sync_worker_unavailable`, with `retry_after`).
  The coordinator ends the run at its last committed page and marks the row
  `idle` with that code. The service defers the source (no strike against the
  failure ceiling). The next run resumes from the cursor. Search keeps working,
  because reads never needed the worker.
* **Worker refuses.** A refused root or authority is a `MappingDeniedError`: the
  run aborts and fails closed.
* **A write that fails inside the worker** for another reason is reported to the
  coordinator as a typed error for that one object.
* **Timeouts.** Connect 5 s. Ping 5 s. A write 600 s. A host call 30 s. An
  agent's start waits up to 15 s for the worker and its embedding model, and the
  first write waits up to 60 s.
* **Dashboard.** `GET /api/knowledge/sync-worker` returns `up`, `restarting`
  (with why) or `down`. The Knowledge Sources tab shows it.

## Credential renewal

Renewal does not move. The arcui renewer and any agent renew under the same
fenced lease in the custody row, so they never both spend one refresh token.
The worker never sees a credential, so it cannot race either of them.

## Proof

* `tests/journeys/test_journey_sync_worker.py`:
  * the child (another pid) writes the documents, and the agent finds them;
  * every SQLite connection this process opens to a connected store is
    `mode=ro`;
  * "Sync now" from the dashboard route reaches the worker;
  * a worker killed mid-sync restarts, and the run resumes from its cursor;
  * the loop's p99 lag, pooled over the whole ingest, stays within 50 ms of the
    same run's idle baseline (and no single freeze passes 500 ms) while the
    worker ingests a large account.
* `packages/arcagent/tests/security/test_sync_worker_abuse.py`, together with
  the protocol, store-authority and delegated-signing tests, is in the
  adversarial battery.
