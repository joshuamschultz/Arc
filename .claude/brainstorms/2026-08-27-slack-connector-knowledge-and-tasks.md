---
topic: "slack-connector-knowledge-and-tasks"
date: 2026-08-27
status: complete
---

# slack-connector-knowledge-and-tasks

## Inspiration

Slack is where most of Josh's real decisions and commitments happen — across many channels and DMs — and they get buried in the scroll. The nightly meeting-ingest workflow just proved the pattern (transcript → tasks in Jira + summary on Telegram + notes in Knowledge). Slack is the other firehose of commitments, and none of it is captured or searchable today. The trigger: if Arc can mine meetings, it should mine the channel where the follow-through actually lives.

## Projects

An Arc connector EXTENSION plus a Slack ingest workflow. Builds directly on prior art: the connector-extensions pattern (.claude/brainstorms/2026-08-04), arcmemory data-source ingestion / connected-knowledge (2026-08-21, SPEC-073, live on DGX), and the nightly-meeting-ingest workflow. Slack already exists as a gateway ADAPTER (chat in/out); this is different — a Slack DATA-SOURCE connector (read history + index) plus the extraction workflow on top.

## Audience

Fleet-wide: every Arc agent can RAG the Slack knowledge, with Josh as the operator. Pain being solved: commitments and context are buried in Slack; there is no memory of what Josh agreed to across channels and DMs, and no way for any agent to search it. Secondary audience: future Josh building his own Slack workflows through arcui or agent chat on top of a solid connection.

## Use Cases

- Nightly digest sweeps Josh's active channels, extracts his tasks → Jira, a tight summary → Telegram, and detailed notes → Knowledge (same shape as meetings).
- Any agent answers 'what did I commit to in #deal-room this week?' or 'summarize the vendor thread' by RAG over all channels + DMs.
- Josh adds the Slack key once (in arcui OR arccli), it saves, and he builds new Slack workflows himself via arcui / agent chat.
- When Josh enables write, an agent can post a summary or reply into Slack — gated and controllable.

## Desired Outcomes

Set up once, always works: add the Slack key via arcui or arccli, it is saved (vault-backed), and the connection self-heals rather than silently dying. Josh's entire Slack history becomes searchable knowledge for the fleet. Nightly commitments are captured without Josh scrolling. Read works out of the box; write is available but OFF by default and toggleable from both arcui and arccli. Nothing fails silently — a broken connection or a dropped sync is visible.

## Guiding Principles

Set-up-once-always-works — the connection is dependable, not fragile (the explicit lesson from tonight's connector flakiness). Surface parity — anything doable in arcui is doable in arccli and vice versa (keys, health, sync, write toggle). Read by default, write operator-gated and controllable from both surfaces. Behaves like the existing connectors (dropbox / jira / confluence) — same enroll → map → sync → search journey, no bespoke path. Fail loud, never silent.

## Constraints

To read private channels and DMs ('everything you're in') Slack requires a USER-scoped OAuth token — a bot token only sees channels the bot is invited to. Fleet-wide access means private DMs become readable by every agent — a real data-exposure surface to weigh. Slack API rate limits bound history backfill. Write reaches real colleagues, so it is an external-comms / lethal-trifecta leg needing approval gates. Keys are vault-backed, never plaintext on disk. Must run through both arcui and arccli, at every tier.

## Scope

**In:** A Slack connector extension: user-scoped OAuth, key save/persist, connection health, arcui + arccli parity. Full-history ingestion → Knowledge for fleet RAG. A nightly extraction workflow: tasks → Jira, summary → Telegram, notes → Knowledge. Read plus operator-controllable write. The canonical connected-source lifecycle (enroll, map, approve, incremental sync, provenance, agent retrieval).

**Out:** Replacing the existing Slack gateway adapter (chat in/out stays). Real-time streaming / live bot auto-responses as the primary path (nightly first). Multi-workspace federation on day one (start with one workspace). Auto-posting to Slack without an operator-approved write path. Building Josh's specific workflows for him — the goal is a connection solid enough that he builds them via arcui / chat.

## Open Questions

- Fleet-wide + DMs: is it acceptable for every agent to read Josh's private DMs, or should DMs be excluded / scoped to josh_agent only?
- Auth: confirm user-scoped OAuth (covers everything) vs bot + per-channel invite, and the exact Slack scopes needed.
- How many Slack workspaces — start with one?
- Write approval model: per-message approval, per-channel allowlist, or a single global on/off toggle?
- History backfill depth (all-time vs last N months) and the rate-limit budget for the initial index.

## Related Solutions

- .claude/brainstorms/2026-08-04-connector-extensions.md — the connector-extension pattern this follows.
- .claude/brainstorms/2026-08-21-arcmemory-datasource-ingestion.md — data-source → Knowledge ingestion (SPEC-073, connected-knowledge live on DGX).
- nightly-meeting-ingest workflow — the extraction shape (tasks → Jira, summary → Telegram, notes → Knowledge) this mirrors.
