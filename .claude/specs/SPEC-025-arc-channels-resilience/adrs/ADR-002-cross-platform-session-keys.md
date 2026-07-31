# ADR-002 — Cross-platform session keys: web/Slack diverge in v1.1

**Status:** Accepted (2026-05-06) — supersedes the relevant claim in SDD §3.3
**Spec:** SPEC-025 (limitation), SPEC-026 (planned resolution)
**Pillar trace:** Modularity (boundary preserved), Simplicity (no platform shim)

## Context

`packages/arcui/src/arcui/routes/chat_ws.py` (lines 22–26) carries a comment
inherited from SPEC-023 claiming:

> `chat_id == session_key`: identical across web/slack/telegram for the same
> (agent, user) pair.

The SPEC-025 implementation audit (architecture review, Major #2) found this
contradicts the actual behavior:

- **`WebPlatformAdapter`** sets
  `session_key = build_session_key(agent_did, user_did)` —
  a SHA-256 of the (DID, viewer-DID) pair.
- **`SlackAdapter._handle_inbound`** sets
  `session_key = f"slack:{channel}:{user_id}"` — a Slack-graph identifier
  with no DID component.

The same human reaching the same agent via web vs. Slack lands in **two
different sessions** with different JSONL files in
`workspace/sessions/<sid>.jsonl` and no shared chat history.

## Decision

**Accept the divergence in v1.1.** Document it explicitly as a known
limitation. Defer cross-platform unified history to SPEC-026.

Concretely:

1. The comment in `chat_ws.py` is wrong but not fixed in this PR — fixing it
   right means resolving the underlying divergence, which is bigger than
   SPEC-025's scope. A grep-able TODO is preferable to a misleading "fix"
   that aligns one half of a half-solution.
2. `tests/integration/test_dual_adapter_chat.py::test_session_keys_intentionally_diverge_across_platforms`
   pins the divergence as intentional. If a future PR accidentally aligns
   the formats, this test fails before the alignment can ship without
   coordinated SessionManager changes.
3. SPEC-026 will introduce a cross-platform identity bridge — likely an
   ARCT trust-issued `user_did` derived from a verified-channel claim —
   that allows both adapters to compute the *same* session key for the
   *same* human regardless of platform. This is the correct fix and
   requires its own design-and-test pass.

## Consequences

**Positive:**

- v1.1 ships without churning the SessionManager or arctrust.
- The limitation is loud (test-pinned), not silent.
- Operators with a single demo audience (one human, web-only) see no
  divergence — the typical demo flow is unaffected.

**Negative:**

- A federal evaluator who tests "talk to the agent in Slack, then in arcui"
  will see two histories. We must mention this in the demo runbook.
- The misleading comment in `chat_ws.py` survives v1.1 — flagged by
  TD-2; a one-line update is queued for SPEC-026.

**Audit posture preserved:** even though sessions diverge, every event
still routes through the same `SessionRouter`, every event carries
`platform="web"|"slack"`, and the audit chain is interleaved correctly per
`tests/integration/test_dual_adapter_chat.py::test_audit_chain_shows_interleaved_platform_events`.

## Verification

- `tests/integration/test_dual_adapter_chat.py::test_session_keys_intentionally_diverge_across_platforms`
- `tests/integration/test_dual_adapter_chat.py::test_audit_chain_shows_interleaved_platform_events`

## Open question (carried into SPEC-026)

Should the cross-platform `user_did` be derived from:

- A SHA-256 of `(operator-issued viewer token, agent_did)` only — what
  `WebPlatformAdapter` does today, but Slack has no viewer token surface.
- An ARCT-issued claim like `did:arc:human:<verified-id>` where each
  channel proves the human's identity (Slack OAuth, magic-link for web,
  Telegram bot-pairing).
- A configurable per-deploy mapping (e.g. `slack_user_id -> viewer_token`).

ARCT issuance is the right answer for federal posture; the others are
either incomplete or operationally fragile.
