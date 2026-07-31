# SPEC-031 — ArcTeam Refresh

**Feature:** Plug-and-play multi-agent communication for Arc.
**Status:** PENDING (specs generated, awaiting implementation)
**Branch:** `arcteam-refresh`
**Type:** Generic (mixed — identity, transport, coordination, CLI, UI)
**Confidence:** High (fast-track) — design decisions locked with the user; codebase investigated at file:line depth.

---

## One-liner

Make it trivial to **create a few agents with identities → put them in a team → have them (and humans) talk and solve problems**, over one signed NATS bus, without hand-wiring config.

## Acceptance demo

```
arc agent create researcher        # DID + @researcher, auto-registered
arc agent create builder
arc agent create critic
arc team create skunkworks --members @researcher,@builder,@critic
arc team up skunkworks             # boots the 3 as serve daemons on the bus
# a human @mentions @researcher a problem → researcher works, @mentions @builder
#   mid-task, builder absorbs it at its turn boundary, critic replies on the channel,
#   human watches every message live in arcui
```

## Principled-coder pillars (governing order)

1. **Simplicity** — NATS/JetStream + arctrust replace hand-rolled cursors, FileBackend, dual CLIs, dual registration. Net LOC down.
2. **Modularity** — hard boundaries: arcagent (one agent) · arcteam (coordinate many) · arcrun (loop exec) · arcui (view) · arccli (commands) · arcgateway (external seam). Contracts explicit in SDD; no task crosses a module.
3. **Security** — DID identity everywhere, Ed25519-signed messages + replay protection, policy-gated steering, audit on every op. Locked-by-default at federal tier via config, not code branches.
4. **Scalability** — shared-nothing NATS bus, durable consumers, supervised daemons; targets 10k+ msg/min across 10–20 coordinated agents.

## Prior work

- Interim combined spec `.claude/specs/arcteam-refresh/SPEC.md` (consolidated into this SPEC-031; interim removed).
- Research artifact (SOTA agent-comms synthesis): https://claude.ai/code/artifact/c3b7fb81-2e44-4e29-9ff1-51d68e5c744b
- Two codebase investigations (arcrun steering; arcteam/gateway/arcui surface) — findings embedded in SDD §"Current state".
- Steering docs: `.claude/steering/{product,tech,structure,roadmap}.md`.

## Key decisions (locked)

| # | Decision | Rationale |
|---|----------|-----------|
| D-1 | Identity = **DID + @handle** for agents AND humans (Slack model) | Human-friendly addressing; @mention = attention; DID stays crypto id |
| D-2 | Transport = **NATS JetStream now**; durable consumer = push-first + inbox-fallback | One primitive covers both; scalability foundation |
| D-3 | **Ed25519-signed** envelope + nonce/ts replay via arctrust | Closes standing security mandate |
| D-4 | Mid-task delivery uses **existing arcrun steering**; `follow_up` default, `steer` for critical+policy-allow | Already built & tested; secure + adopt, don't rebuild |
| D-5 | **arcui = view only** (read-only bus subscriber + input forward); 1:1 stays arcagent, coordination is arcteam | Don't mix concerns; reconciles SPEC-026 (single-bus subscriber ≠ banned parallel push) |
| D-6 | Retire FileBackend, legacy slack/telegram modules, dual CLI, dead UIReporter stub | Drop unused; no compat shims |
| D-7 | Defer external Slack/Teams/Telegram bridging, consensus/coordinator, goal system | YAGNI; separate builds |

## Known bug this fixes

`arc agent create` registers `id=name` while the messenger expects `agent://name` → messages silently DLQ as `sender_unauthorized`. Unifying on DID-keyed identity (REQ-003) fixes it.

## Files

- `PRD.md` — requirements (EARS, pillar-tied acceptance, MoSCoW)
- `SDD.md` — module boundaries + component design
- `PLAN.md` — phased tasks (TDD, one module per task)

## Learnings

_(captured during /implement and /review)_
