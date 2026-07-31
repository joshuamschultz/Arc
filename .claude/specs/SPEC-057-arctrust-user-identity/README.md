# SPEC-057 — arctrust User Identity, Pairing & the Context-Resolved Trifecta

**Status:** PRD draft (pending review)
**Owner:** Josh
**Depends on:** SPEC-035 (mechanical operator approval), SPEC-053 (audit authority independence), SPEC-056 (mission-control dispatch)

## Why

Deployed agents freeze and hit max-turns because the lethal-trifecta `HumanGate`
blocks each gated tool call for 300s waiting for an operator who cannot approve —
the pending-approval surface isn't reachable, the owner-channel exemption is a
brittle `user://operator` string match, and there is no first-class notion of the
human *user* behind arcui / Telegram / Slack. This spec introduces a real user
identity in arctrust, pairs a user's external surfaces to their workspace, and
makes each trifecta leg a **context-resolved definition** so the gate blocks
actual exfiltration without smothering ordinary research-and-write work — **without
weakening the trifecta**.

## The one-line model

> The trifecta still fires. We never bypass it. We only (1) define each leg by its
> *real* condition and (2) make approvals actually grantable.

| Leg | Fires on | Not a leg when |
|---|---|---|
| `private_data` | any read of on-machine data (workspace, `.env`, tomls, traces, agent files, memory, profile, recall) | never |
| `external_comms` | outbound to a **non-owner** | counterparty is the **paired owner** |
| `untrusted_input` | ingesting **unvetted** content | source is **operator-vetted** (per-tier URL trust) |

## Documents

- [PRD.md](PRD.md) — requirements (this is the drafted artifact)
- SDD.md — *pending PRD approval*
- PLAN.md — *pending SDD*

## Related code (today)

- `packages/arctrust/src/arctrust/policy.py` — `GlobalLayer.forbidden_composition`
- `packages/arcagent/src/arcagent/core/session_internal/capability_ledger.py` — leg map + `legs_for_call` (owner-channel hook)
- `packages/arcagent/src/arcagent/tools/human_gate.py` / `approval_channel.py` — the 300s gate + arcstore pending channel
- `packages/arcagent/src/arcagent/modules/web/url_policy.py` — reachability allowlist (needs denylist mode + trust verdict)
- `packages/arcui`, `packages/arccli` — approval surfaces + token-URL auth to retire
