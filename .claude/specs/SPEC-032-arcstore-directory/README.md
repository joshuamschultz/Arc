# SPEC-032 — ArcStore Team Directory + arcui Integration

**Feature:** Make arcstore the durable team directory, wire arcui to it, and let humans interact with agents via channels/DMs.
**Status:** PENDING
**Branch:** `feat/SPEC-032-arcstore-directory` (off `fix/arcteam-live-daemon-bus`)
**Type:** Generic (directory storage + UI integration + CLI + human interaction)
**Confidence:** High — architecture decided with the user; both arcstore and arcui data planes investigated at file:line depth.

---

## One-liner

Give the team a **single durable directory** (entities · teams · channels · membership) in **arcstore**, wire **arcui** to read it and observe the live bus, and let a **human** manage channel membership and chat with agents via channels and DMs — from the UI and the CLI.

## Why (the problem)

SPEC-031 shipped the signed NATS bus + steering + autonomous conversation, but it registered the entity/team directory into **NATS KV**, while **arcui reads its roster from a disk scan of `team_root/*_agent/` and reads history from arcstore** — two disconnected data planes. Result: arcui shows **no agents, no channels, no fleet**, and `arc ui start` never even constructs a `messaging_service`. This spec closes that gap.

## Decision (approved with Josh)

**arcstore becomes a two-plane store; NATS stays transport-only.**

- **Immutable plane** (exists today, untouched) — traces, logs, audit, telemetry. Insert-once (`INSERT OR IGNORE`), immutable by design.
- **Mutable plane** (new, general-purpose) — keyed records with **update + delete**: agent configs, settings, and the **team directory** (entities · teams · channels · membership). SPEC-032 builds this plane and makes the **team directory its first tenant**; agent configs/settings are future tenants that reuse it without redesign.

Rationale: one durable source of truth per concern (Simplicity); directory/config is authorization-relevant, queryable, auditable state that fits arcstore's durable role (Security); arcui already reads arcstore (native fix). arcteam ends with **two backends by concern**: directory → arcstore mutable plane, message streams + durable consumers → NATS.

## Scope (this spec: phases 1–3)

1. **Directory in arcstore + wire arcui** — fix the empty UI (agents, channels, fleet appear).
2. **Channel membership control** — CLI + arcui manage membership; running daemons re-subscribe on change.
3. **Human interaction** — human as a first-class signed entity; DM + post-to-channel from arcui composer and CLI.

**Out of scope (SPEC-033, later):** Slack mirroring (gateway ↔ arcteam bridge). This spec's directory + channel model is designed so Slack maps cleanly onto it later.

## Principled-coder pillars

1. **Simplicity** — one durable directory; arcui plumbing already exists (data-starved, not missing) — mostly wiring.
2. **Modularity** — new `arcstore.directory` is separate from the immutable telemetry backend; arcteam splits directory vs stream backends behind its existing Protocol; `MessagingService` untouched; arcui is view+forward only.
3. **Security** — membership is an access-control decision, now durable + auditable in arcstore; human posts are Ed25519-signed; arcui `/api/*` stays token-gated.
4. **Scalability** — SQLite WAL shared-nothing directory; NATS for message fan-out; read-on-demand roster (no live polling storm).

## Key facts (from investigation)

- arcstore = SQLite, **insert-once** (`INSERT OR IGNORE`, no update/delete). Mutable membership needs a new `arcstore.directory` module (`ON CONFLICT DO UPDATE` + delete — pattern already in `sync_state`).
- arcteam `StorageBackend` is two protocols fused: **directory** (`read/write/delete/query/list_keys/exists`) vs **stream** (`append_auto_seq/read_stream/open_consumer`). Split along that line.
- arcui `create_app` already accepts `messaging_service` + `team_post_forwarder`; lifespan spins up `TeamBusObserver`; frontend `pages/messages.tsx` already has the channel switcher + composer. **`arc ui start` (ui.py:222-227) passes neither** → the one root wiring gap.
- Roster = disk scan (`arcgateway.team_roster.list_team`), not the arcteam registry; `agent_registry` is only a liveness overlay.

## Files

- `PRD.md` — requirements (EARS, pillar-tagged, MoSCoW)
- `SDD.md` — module boundaries + component design (arcstore.directory, composite backend, arcui wiring)
- `PLAN.md` — phased tasks (TDD, one module per task)

## Learnings

_(captured during /deepen, /implement, /review)_
