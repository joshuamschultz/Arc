# Arc Problem Log

The canonical, in-repo record of problems hit in production and how they were
resolved — so we can learn from them and analyze for patterns later. This is the
home for **problems**; **decisions** go in [`decisions-log.md`](./decisions-log.md);
reusable solutions go in [`solutions/`](./solutions/).

Nothing about the project should live only in a session transcript, a private
Claude memory, or someone's head. When a problem is diagnosed or fixed, add an
entry here.

## Entry format

```
### PROB-NNN — one-line title
- **Date:** YYYY-MM-DD · **Area:** <package/subsystem> · **Status:** FIXED | MITIGATED | OPEN
- **Symptom:** what was observed (with numbers where possible).
- **Root cause:** the actual mechanism, verified.
- **Fix:** what changed (commit hash) or what remains.
- **Lesson:** the transferable takeaway.
```

Status: **FIXED** (root cause removed + verified), **MITIGATED** (bleed stopped,
root fix pending), **OPEN** (diagnosed, not yet fixed).

---

## Problems

### PROB-013 — Memory consolidation ran in a loop, burning $159/day on one agent
- **Date:** 2026-08-29 · **Area:** arcagent/modules/memory · **Status:** FIXED
- **Symptom:** coder_agent made 1,682 claude-sonnet-5 calls in 24h (43% over 100k tokens, max 235k), 137M input tokens, **$159/day** — 99.7% of fleet cost — while "idle". Other agents: 1-2 calls/day.
- **Root cause:** `_capture` advanced the consolidation trigger (`events_since_consolidate`) and reset the idle clock on EVERY captured turn, including the agent's own background self-wakes (pulse, proactive, consolidation itself). A quiet agent kept crossing the 20-event threshold and re-running an 11-15 call agentic consolidation ("You are the memory of an agent…") on nothing new. The `automated` flag doesn't distinguish these (pulse dispatches `automated=False`). It was memory consolidation, not the workpad — the `coder_agent/workpad` label misled.
- **Fix:** new per-turn marker `turn_context.interactive()` (True only for a turn a real person opened), set in `bind_inbound_channel` from `dispatch_stream(interactive=…)`. `_capture` still captures everything but advances the consolidation trigger only on interactive turns (commit 660b727c). Verified: coder 0 calls / $0.00 over 8 min after the fix.
- **Lesson:** a cadence that counts an agent's own background activity as "new input" self-perpetuates. Background self-wakes must be distinguishable from real interaction, and only real interaction should drive expensive background work.

### PROB-012 — Distiller ran on the top model fleet-wide
- **Date:** 2026-08-29 · **Area:** arcagent memory config / arccli templates · **Status:** FIXED
- **Symptom:** every agent's memory-consolidation distiller was configured to claude-sonnet-5 — the most expensive model — for an unattended background summarizer.
- **Root cause:** the agent-create templates hardcoded a Sonnet distiller; deployed configs inherited it.
- **Fix:** set `distill_model = claude-haiku-4-5-20251001` on all 6 DGX agents (backups `.bak-haiku`) and in the new-agent templates `arccli/commands/init.py` + `agent/_common.py` (commit 64b07332).
- **Lesson:** background/unattended LLM work should default to a cheap model. A top-model default is a cost bug waiting for an active agent to trigger it.

### PROB-011 — Chat file upload fails "agent workspace unavailable"
- **Date:** 2026-08-29 · **Area:** arcui / arcgateway messaging · **Status:** OPEN
- **Symptom:** uploading an image to an agent in web chat fails with "agent workspace unavailable".
- **Root cause:** not yet root-caused. Strongly suspected to be collateral from PROB-013 (the box was saturated by coder's runaway); may clear now that coder is fixed. Needs a re-test.
- **Fix:** pending — re-test upload after PROB-013; if it persists, trace the workspace-write path in the chat upload handler.
- **Lesson:** one agent's runaway load degrades unrelated surfaces; rule out systemic load before chasing a feature-specific bug.

### PROB-010 — Dashboard panels "Failed to fetch" and slow (2-5s+)
- **Date:** 2026-08-28 · **Area:** arcui/observe · **Status:** FIXED
- **Symptom:** Activity, Approvals, and Model-usage panels showed "Couldn't load data / Failed to fetch" and took 2-5s+ to load.
- **Root cause:** all three read through ONE shared asyncpg pool (max 10). `Observe.runs()` folded 3×20,000 = 60,000 JSONB rows every ~4s poll — dragging loads to multi-second and starving the pool until sibling panels' unhandled query timeouts surfaced as 500s.
- **Fix:** cut the runs scan to 4k/table (still far more than the 200 runs shown) and wrapped the three reads in try/except → clean 503 instead of an unhandled 500 (commit 23046e1c).
- **Lesson:** a read that scans tens of thousands of rows on a shared pool at a fast poll cadence is a self-inflicted DoS on every sibling reader. Bound scans; degrade gracefully.

### PROB-009 — Connector sync card shows 0 batches / 0 bytes / "Never" for indexed sources
- **Date:** 2026-08-27 · **Area:** arcagent/connected_data + arcmemory + arcstore · **Status:** OPEN
- **Symptom:** a source with content actually indexed still shows "Last sync: Never", "Batches read: 0", "Downloaded: 0 B".
- **Root cause (verified):** (1) `_status_wire` reads `last_synced_at`, an attribute that exists on none of `SourceRuntimeStatus` / `SyncState` / `SourceSyncState` → always None → "Never". (2) pages/bytes are transfer counters, cumulative+durable in arcstore's `connected_source_sync` collection, but that collection had 0 rows in the observe DB — the two-DB split (agent runtime arcstore DSN ≠ observe/arcui DSN) means the card reads state from a DB the sync never wrote. (3) the real indexed count (`DocIndex.list_documents`) is unwired to the card.
- **Fix:** pending (needs a schema field + migration for `last_synced_at`, reconciling the two-DB DSNs, and wiring an index-derived count). Scoped, not a tail-end patch.
- **Lesson:** a status field must be written by the same path and DB the reader reads; a "0" from a defaulted-empty row looks identical to real zero.

### PROB-008 — Confluence + Slack connections show "failed" though healthy
- **Date:** 2026-08-27 · **Area:** arcagent/connected_data · **Status:** MITIGATED
- **Symptom:** confluence and ctgslack cards show "failed / type unknown / 0 bytes / Never".
- **Root cause:** both connectors probe fully reachable (auth + tools work). The "failed" is stale status from before PROB-014's mapping fix; sync never re-succeeded to clear it. Could not capture a live sync error headless (needs the Retry button).
- **Fix:** mappings are healed (PROB-014); a Retry or the next periodic sync should clear it. Follow-up: make failed status not sticky when a re-sync succeeds.
- **Lesson:** distinguish "connection broken" from "stale failed status" — a probe proves the former is fine.

### PROB-007 — Workflow signing dead-end: can't approve OR remove a stale request
- **Date:** 2026-08-28 · **Area:** arcui/routes/approvals · **Status:** FIXED
- **Symptom:** approving a `workflow_sign` request failed with `workflow_changed_since_the_request`; stale requests piled up with no way to clear them.
- **Root cause:** the sign-request binds to the definition's content hash; once the workflow is edited the hash no longer matches, so approving is (correctly) refused — but nothing removed the now-un-approvable request.
- **Fix:** `list_approvals` now reaps a stale `workflow_sign` request — if its hash no longer matches the current definition, it resolves to `expired` and drops from the queue. Deny still clears a live one; a fresh request for the current version stays approvable (commit 23046e1c).
- **Lesson:** any approval bound to a mutable artifact needs a self-removal path when the artifact changes; a correct refusal without an exit is a dead-end.

### PROB-006 — Workflow runs listed in a confusing (non-chronological) order
- **Date:** 2026-08-28 · **Area:** arcteam/workflow · **Status:** FIXED
- **Symptom:** the workflow Runs tab showed runs in opaque insertion order.
- **Root cause:** `WorkflowRunStore.list_for_workflow` returned rows unsorted; the JSON-blob store has no SQL ORDER BY for a nested field.
- **Fix:** sort newest-first by `created_at` (ISO string) at that one reader seam (commit 23046e1c).
- **Lesson:** sort at the single seam every surface reads through, not per-surface.

### PROB-005 — Gmail multi-account: every Google sync failed "missing --account"
- **Date:** 2026-08-27 · **Area:** extensions/google_workspace (gog CLI) · **Status:** FIXED
- **Symptom:** with two Google accounts signed into gog, every Gmail sync failed; the source showed type "unknown".
- **Root cause:** the connector hardcoded a single nameless account and passed no `--account`; gog refuses a bare read once more than one token exists.
- **Fix:** a per-connection non-sensitive `account` field placed into `GOG_ACCOUNT` (exported into every gog process this connection starts, nowhere else). Optional — a single-account host places nothing and still works (commits e4a1b24e, f18c53b6).
- **Lesson:** a connector wrapping a multi-account CLI must bind each connection to one account via an env placement, not a per-call flag threaded through the sync path.

### PROB-004 — gog keyring unreadable in a headless service
- **Date:** 2026-08-27 · **Area:** deploy / google_workspace · **Status:** FIXED (2026-08-29)
- **Symptom:** gog errors "no TTY available for keyring file backend password prompt".
- **Root cause:** gog keeps tokens in a file keyring that needs a password; a background service has no TTY to prompt.
- **Fix:** `GOG_KEYRING_PASSWORD` set in `~/arc/config/arc.env` (0600; `scrubbed_environment` inherits it into the gog child). Both accounts' `account` bound per connection in `connections.env` (`ARC_SECRET_BLACKARC_ACCOUNT` = josh@blackarcindustrial.com, `ARC_SECRET_SYSTEMS_ACCOUNT` = josh@blackarcsystems.com). Both connections now probe reachable with full Gmail/Drive/Calendar tools.
- **Lesson:** vendor CLIs with an interactive keyring need an explicit headless credential path in the service environment; the binary must also be on the service PATH (`~/.local/bin`).

### PROB-003 — Connected knowledge was invisible to the agent
- **Date:** 2026-08-27 · **Area:** arcagent/connected_data + memory · **Status:** FIXED
- **Symptom:** an agent answered "nothing in connected sources references X" while 47 indexed Slack channels sat unsearched.
- **Root cause:** the search tools (`document_search`, `datastore_query`, `connected_sources`) were pull-only; nothing told the agent its connections existed.
- **Fix:** the connected-data module injects a `connections` catalog into the prompt (names, kinds, status, homes) plus a nudge to search before declaring something unknown — module-owned, so no connectors means nothing injected. Added per-agent retrieval settings (`doc_search_top_k`, `doc_search_min_score`) (commit 53e16715).
- **Lesson:** a capability the model can't see, it won't use; surface a lean catalog of what's reachable, don't rely on the model to remember pull-only tools.

### PROB-002 — Slack search died "client connection closed" after a sync
- **Date:** 2026-08-27 · **Area:** extensions/slack · **Status:** FIXED
- **Symptom:** `slack_search` failed with "client connection closed" after a sync ran.
- **Root cause:** `close_source` closed the shared httpx client; later tool calls reused the dead client.
- **Fix:** `_http()` re-opens the client if closed; tool calls self-heal (deployed 2026-08-27).
- **Lesson:** a shared client that a lifecycle method closes must be lazily re-opened by every user, not assumed live.

### PROB-001 — Approved data-source mappings expired on a 24h timer
- **Date:** 2026-08-27 · **Area:** arcmemory/connected_data + mapping · **Status:** FIXED
- **Symptom:** every connector (Slack, Confluence, Dropbox) reverted to "awaiting mapping / 0 bytes" ~a day after approval. Looked like storage lost on deploy; it wasn't.
- **Root cause:** `stage_mapping_proposal` stamped `expires_at = now + 24h`; `require_approved_mapping` skipped any row past `expires_at` BEFORE checking if it was approved, so an operator-approved mapping silently expired. On DGX, 41 of 44 approved grants were aged past the timer.
- **Fix:** an approved mapping is durable — expiry bounds only the pending (unacted) window; a structural change re-triggers approval via the hash, never a timer. Fixed all three read sites (commit 81d471cc). Heals retroactively.
- **Lesson:** an approval's expiry protects the pending request, never the granted state. Any "is this still approved?" check that ANDs in an expiry on an already-approved row silently un-grants on a clock.

---

## Recurring lessons (patterns across sessions)

Transferable engineering lessons that keep re-appearing. Prefer these when
building; violating one is usually how a PROB above happened.

- **Verify through the real surface, not green tests.** Green suites have hidden ~13 defects (SPEC-061); drive the actual buttons / real path before claiming done.
- **Test what users do, not what's adjacent.** Journey tests should fake only the LLM wire; the untested shape is usually the bare, minimal call.
- **Producers-unwired is the default failure.** A shipped engine with zero callers (SPEC-073 Dropbox ingestion; the media pipeline `to_model_content`; the distiller) recurs — demand an E2E-through-the-real-path test, never trust self-reports.
- **Background work must be cheap and gated.** Cadence counters must persist (in-memory "every N turns" dies on restart); background self-wakes must not drive expensive work (PROB-013); unattended jobs default to a cheap model (PROB-012).
- **An approval/grant is durable once granted.** Expiry gates the pending window, not the granted state (PROB-001); a grant bound to a mutable artifact needs a self-removal path (PROB-007).
- **State must be written and read through the same path + DB.** The two-DB split silently zeroes the connector card (PROB-009); a status field no writer sets reads as a permanent default (PROB-009 `last_synced_at`).
- **Bound every scan; degrade, don't 500.** A fast poll over tens of thousands of rows on a shared pool DoSes its siblings (PROB-010).
- **Surface capabilities the model can't otherwise see** — a lean catalog beats hoping the model remembers pull-only tools (PROB-003).
- **Distinguish "broken" from "stale".** A probe proves a connector healthy even when its card says failed (PROB-008).
- **One reader seam owns ordering/shape** so every surface agrees (PROB-006).
- **Deploy always from main;** sub-agents must never run destructive git (reset/stash wipes parent work); stage explicit paths, never `git add -A` on a shared tree.
- **Degrade LOUD, never silent** — a silent embedder/degrade indexes keyword-only and looks fine; dedup must be LLM-confirmed.
- **Agents must not share a workspace;** an agent's brain writes to its own workspace via direct I/O, never via LLM tools (ADR-029).
