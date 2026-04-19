# arcgateway Multi-Instance

Single-instance is the default and the only fully supported deployment today. M1 arcgateway is **not** horizontally scalable without external help.

## What breaks first (1 → N)

| Subsystem | Problem | Mitigation |
|---|---|---|
| **Telegram polling** | One process per bot token; two pollers race silently | Webhook mode + sharded routing (M3), or one bot token per instance |
| **`SessionRouter._active_sessions`** | In-memory dict diverges across instances | Sticky routing OR shared `SessionIndex` via FTS5 store (already file-backed — multi-instance-ready with NFS; M2 Postgres backend cleaner) |
| **`PairingStore`** | SQLite on local disk — codes minted on instance A can't be approved via instance B | M3: Postgres backend; federal multi-instance already stubbed (`PostgresPairingStore`) |
| **`IdentityGraph`** | Same as PairingStore — SQLite locally | Same mitigation |
| **Typing indicators** | Double-fire if routing isn't sticky | Sticky routing |
| **Streaming edits** | Two instances editing same platform message → garbled | Per-message-id claim token (M2 at single-instance; M3 distributed) |
| **httpx connection pool** | Per-adapter client already sharded; federal subprocess isolation gives per-session pool | No action needed |

## Three routing patterns (when you need scale)

### A. Sharded webhooks (consistent hash on `chat_id`)

Load balancer routes each platform webhook by `hash(chat_id) % N`. Works for Slack (webhook mode), Discord, any webhook platform. **Does not work for Telegram polling** — switch Telegram to webhook mode first.

### B. NATS JetStream subject-per-session (recommended at scale)

Subject: `arc.session.{session_id}`. Durable consumer = gateway-instance-id. Session ownership migrates cleanly — JetStream persists messages until acknowledged, so rolling updates don't drop anything.

Requires NATS infrastructure. Federal deployments already use NATS for inter-agent messaging (per arcagent CLAUDE.md), so this is the recommended M3 path.

### C. Redis pub/sub + Postgres session state

Session state in Postgres with row-level locks on `session_key`. Redis pub/sub for presence. Less persistence than JetStream (lost messages during subscriber churn) — not recommended for federal.

## Capacity (from deepening research)

| Isolation mode | RSS per session | Cold start |
|---|---|---|
| asyncio task (personal/enterprise) | 2–8 MB | 10–50 ms |
| subprocess (federal, `arc-agent-worker`) | 35–60 MB | 150–400 ms |
| Firecracker microVM (future) | 128 MB | 125 ms |
| NATS-routed remote agent (M3) | — | +10–30 ms RTT |

A single federal-tier instance with 8 GB of usable RAM and default subprocess limits sustains roughly 100 concurrent paired sessions before subprocess memory is the bottleneck. Per-session CPU limit (60 s) prevents runaway tool loops from monopolizing one core.

## Current M1 status (honest)

- Single-instance only.
- `GatewayRunner.from_config()` loads TOML and selects executor by tier. No cluster membership, no leader election.
- NATS routing is **not** implemented. `NATSExecutor` exists as a `NotImplementedError` stub.
- `PostgresPairingStore` is a stub.
- `SessionIndex` is file-backed + FTS5 — safe to share across instances via NFS; not tested multi-instance.

If you need multi-instance before M3:
1. Front with a sticky load balancer (`chat_id` consistent hash).
2. Share `SessionIndex` database via NFS (only ONE indexer writes; readers are safe).
3. Accept that pairing codes must be approved on the originating instance until Postgres backend lands.
4. One bot token per instance (Telegram).
