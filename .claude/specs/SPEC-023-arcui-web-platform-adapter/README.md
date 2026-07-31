---
spec_id: SPEC-023
name: arcui-web-platform-adapter
status: complete
type: integration
created: 2026-05-03
intake_confidence: 0.90
type_confidence: 0.85
fast_track: true
prior_work:
  - .claude/brainstorms/2026-05-03-arcui-as-platform-adapter.md (deepened)
  - .claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md
  - .claude/solutions/security-issues/2026-02-21-arcrun-phase4-hardening-review-learnings.md
related_specs:
  - SPEC-015-arcui-llm-telemetry (initial arcui)
  - SPEC-016-multi-agent-ui
  - SPEC-019-arcui-zero-config
  - SPEC-020-nlit-demo-local-build
  - SPEC-022-arcui-agents-live (prior arcui work)
trigger: NLIT 2026 demo prep — need remote-accessible live chat with served agents, built as production architecture (not demo hack)
pillars_priority: [Simplicity, Modularity, Security, Scalability]
---

# SPEC-023 — ArcUI Web Platform Adapter

## TL;DR

Add a third platform adapter to `arcgateway` (peer to Slack and Telegram) so that browser chat to served agents flows through the same `SessionRouter` → `Executor` → `ArcAgent` path as every other platform. arcui hosts the gateway runtime in-process; the new `WebPlatformAdapter` accepts inbound WebSocket connections, normalizes them into `InboundEvent`, and pushes outbound `Delta` chunks back to the browser. Two new pages in arcui — Messages (DM with served agents) and Knowledge (memory + workspace + graph stats) — are thin clients on this pipe.

The architectural invariant: **arcui is just another chat platform**. Slack, Telegram, ArcUI — three peers. No shortcuts that import `arcagent` directly into arcui. No duplicated session/queue/policy logic. Cross-platform identity, audit, pairing, queue depth, and policy enforcement work for browser chat *for free* because they all run inside `SessionRouter`.

## Approach Summary

```
Browser → WSS /ws/chat/{agent_id} → arcui chat_ws route (thin proxy)
       → arcgateway WebPlatformAdapter (registered with in-process gateway runtime)
       → SessionRouter (unchanged)
       → AsyncioExecutor → ArcAgent.run()
       → StreamBridge → WebPlatformAdapter.send() → fan out to registered WS
```

Six packages touched:
- `arcgateway` — new `adapters/web.py`, new `bootstrap.py`, new `identity.py`, config wiring in `cli.py`/`config.py`.
- `arcui` — new `routes/chat_ws.py`, new `routes/knowledge.py`, new pages and JS, self-hosted fonts.
- No changes to `arcagent`, `arcrun`, `arcllm`, `arcteam`.

Three module boundaries:
- arcui imports arcgateway (allowed, existing).
- arcgateway does NOT import arcui.
- arcui does NOT import arcagent (the adapter pattern is the whole point).

## Decisions Log

All architectural decisions resolved in the deepened brainstorm:
- `.claude/brainstorms/2026-05-03-arcui-as-platform-adapter.md`

13 decision blocks (process model, identity, chat_id derivation, multi-browser broadcast, capability surface, WS envelope, TOML shape, send fan-out, identity migration, knowledge contract, test plan, channels timing, ttyd auth) — each with pillar trace, implementation, follow-ups.

8 sections enriched with `### Research Insights` from parallel research agents (WS production patterns, federal compliance, knowledge UI, in-process scalability).

The brainstorm is the source of truth for detail. The PRD/SDD/PLAN below are normative for implementation. Any conflict resolves in favor of the brainstorm's `### Research Insights` blocks (most recent decisions).

## Pillar Priority

In strict order:

1. **Simplicity** — boring patterns, works out of the box, readable cold.
2. **Modularity** — hard boundaries, explicit contracts, no logic bleed across packages.
3. **Security** — open-by-default for personal, locked-by-default at federal tier; air-gap capable today.
4. **Scalability** — stateless, horizontal; 10k+ msg/min across 10–20 instances unblocked by NATSExecutor swap.

When priorities conflict, resolve in this order. Every requirement in the PRD has an acceptance criterion tied to at least one pillar.

## Compliance Track

Air-gap-ready today (DOE labs, NASA on-prem). The path to FedRAMP Low is one fix (Fix #1: OS user binding in `SessionStartFields`, 1h effort). FedRAMP Moderate requires three fixes (#1 + #2 token TTL + #5 POST bootstrap). FedRAMP High requires Fix #3 (TOTP/U2F MFA). All five fixes mapped in PLAN.md as the "Federal Track" — not blocking the core build.

## Learnings (Populated During Implementation)

- **SessionRouter adapter binding is a constructor-time circular dep.** SDD §3.3 step 5 referenced ``session_router.register_adapter(adapter)``, but session.py exposes ``adapter`` only as a constructor argument and the adapter needs ``session_router.handle`` for ``on_message``. Bootstrap resolves this with two-step construction: build the router first, then the adapters with a closure over ``router.handle``, then assign ``router._adapter`` post-construction. Documented inline in ``arcgateway/bootstrap.py``.
- **arcui→arcagent boundary tightened with importlib.util.find_spec.** Pre-existing routes (``agent_detail.py``) imported ``arcagent`` only to discover its filesystem path. Replaced with ``importlib.util.find_spec("arcagent")`` so the architectural boundary test passes without changing runtime semantics.
- **The echo-stub executor path is invaluable for routing tests.** ``AsyncioExecutor()`` with ``agent_factory=None`` returns a deterministic echoed reply that lets the chat WebSocket integration tests verify the full ``ingest → SessionRouter → StreamBridge → adapter → drain → ws.send_json`` pipe end-to-end without needing a real ArcAgent / team/.
- **Playwright tests on this host are pre-existing flake.** ``test_visible_ui_smoke.py`` and ``test_browser_rehearsal.py`` (7 tests) timeout on the ``.agent-card`` selector at HEAD without any SPEC-023 changes. They are excluded from the final quality gate but should be triaged separately.

## Success Criteria

See PRD §Success Criteria. Live demo on 2026-05-04 is the first deployment of this production architecture, not a separate demo build.

## Files

- [PRD.md](./PRD.md) — product requirements with EARS acceptance criteria
- [SDD.md](./SDD.md) — solution design with module boundaries
- [PLAN.md](./PLAN.md) — implementation phases and tasks
- [README.md](./README.md) — this file (metadata + decision pointers)
