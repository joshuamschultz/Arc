# SPEC-020 — Rehearsal & Run-of-Show

**Talk:** NLIT 2026, Kansas City, May 4–7
**Demo:** "From Chatbots to Coworkers" — two independent Arc agents, two modes
**Tier:** personal (federal-ready as product seed; see PRD §4 NFR-4)

---

## What the audience sees

| Window | Content |
|---|---|
| **Left (projector)** | Obsidian — `team/nlit_soc_agent/workspace/` opened as a vault, graph view active, **Refresh Any View** plugin enabled |
| **Right (projector)** | arcui dashboard at `http://localhost:8420` |
| **Off-screen** | Terminal where the SOC agent is being chatted with (fed by anchor prompts) |

**Act 1 (live, ~6 min):** I chat with `nlit_soc_agent` about STIG findings. Audience watches typed entities materialize in Obsidian's graph + tool call timeline in arcui.

**Act 2 (walkthrough, ~3 min):** I switch focus to `nlit_cora_agent`'s workspace. Open the `Reports/CORA-Gap-Report-{date}.md` that was written overnight by the cron. Show the Schedule History card in arcui — last fire time, "ok" status, elapsed seconds. The brain ran on its own.

---

## Five-rehearsal pre-flight (T-7d → AM-of)

### Rehearsal 1 — Baseline (T-7d)

Run the 5 anchor prompts (PRD §7) in order against `nlit_soc_agent`. Goal: ≥4 of 5 fire `write_entity`.

```bash
arc agent run team/nlit_soc_agent "We just had a meeting about the RHEL upgrade on host-alpha — Jane Doe is the system owner."
arc agent run team/nlit_soc_agent "SIEM flagged 47 SSH auth failures on host-alpha at 23:14 UTC last night."
arc agent run team/nlit_soc_agent "Now I'm seeing a DNS anomaly on host-beta at 23:52 UTC. Both hosts are on subnet 10.0.1."
arc agent run team/nlit_soc_agent "STIG V-220812 was flagged on host-alpha — it's a CAT I, control family CM, CCI-000366."
arc agent run team/nlit_soc_agent "I'm escalating this to a P1 incident — possible lateral movement between host-alpha and host-beta."
```

Document in this file: how many fired correctly, what was off, what changes (if any) you made to identity.md or the anchor wording.

If <4/5 fire: revise the **Tool Triggers** section in identity.md or the entity-extraction skill. Re-run.

Also kick off a CORA cron run manually (to verify the pipeline works at all) and confirm the gap report contains V-220812 in section 1:

```bash
arc agent run team/nlit_cora_agent "Run the CORA pipeline by calling run_cora_pipeline. Do not call other tools."
grep -c "V-220812" team/nlit_cora_agent/workspace/Reports/CORA-Gap-Report-*.md   # expect ≥3
```

### Rehearsal 2 — Variation (T-5d)

Same 5 anchors but rephrased the way you'd naturally vary on stage. Confirm tool still fires. Confirm wikilinks: when you mention host-beta after host-alpha, the new entity has `[[host-alpha]]` in the wikilinks list.

### Rehearsal 3 — Recovery (T-3d)

Deliberately use one ambiguous turn that you expect might miss the tool. Practice the recovery line out loud:

> "Log that — host-alpha, subnet 10.0.1, type System."

If the agent recovers reliably with that imperative reformulation, you're stage-ready. Time the full demo: under 8 minutes including 2 deliberate recoveries.

### Rehearsal 4 — Environment (T-1d)

Run the full demo from the actual demo machine on the actual venue network (or tethered hotspot). Confirm:

- `ANTHROPIC_API_KEY` is set in `/Users/joshschultz/Projects/arc/.env` (or wherever the dev machine reads dotenv from)
- `arc ui start` opens the dashboard at `localhost:8420`; trace stores include `nlit_soc_agent` and `nlit_cora_agent` (`Trace stores: 7+`)
- Obsidian's **Refresh Any View** plugin is enabled (Settings → Community Plugins)
- Production cron schedule still active: `arc agent run team/nlit_cora_agent "schedule_list"` shows `sched_80f11ca90677` enabled, expression `0 4 * * *`
- Latency check: a `say hello` round-trip to Claude completes <5s

Have a secondary network (phone hotspot) ready in case venue WiFi is hostile.

### Rehearsal 5 — Dress (AM-of)

Full uninterrupted run-through with someone watching. No corrections. If a tool call fails, **observe and document; do not restart**. This rehearsal sets the floor expectation for stage. If anchors land 4/5 here, they'll land on stage.

---

## Stage setup checklist (10 minutes before talk)

```bash
# 1. Stop anything left over from rehearsal
pkill -f 'arc agent serve' 2>/dev/null
PID=$(lsof -iTCP:8420 -sTCP:LISTEN -t 2>/dev/null); [ -n "$PID" ] && kill $PID

# 2. Clean SOC entities — start with a fresh vault for the audience
rm -rf team/nlit_soc_agent/workspace/entities/*

# 3. Trigger one CORA pipeline so the report is fresh and dated today
arc agent run team/nlit_cora_agent "Run the CORA pipeline by calling run_cora_pipeline. Do not call other tools."
ls -la team/nlit_cora_agent/workspace/Reports/   # confirm today's date

# 4. Start arcui (will open browser to localhost:8420 with viewer token)
arc ui start &

# 5. Open Obsidian on team/nlit_soc_agent/workspace/ as the vault. Pin graph view.

# 6. Open a terminal for SOC chat
arc agent chat team/nlit_soc_agent
```

Index card at the podium with the 5 anchor prompts in order. Recovery line memorized:

> *"Log that — {name}, {properties}, type {Type}."*

---

## Run-of-show (~10 min total)

### Act 1: Conversational entity capture (live, ~6 min)

| Beat | What you say | What the audience sees |
|---|---|---|
| Setup | "Watching what happens in Obsidian as I describe a security incident." | (Obsidian graph empty) |
| Anchor 1 | RHEL upgrade meeting on host-alpha, Jane Doe is system owner | host-alpha + Jane Doe nodes appear, edge between them |
| Anchor 2 | 47 SSH auth failures on host-alpha at 23:14 UTC | Event node, edge to host-alpha |
| Anchor 3 | DNS anomaly on host-beta at 23:52 UTC, both on subnet 10.0.1 | host-beta node + edge to host-alpha (same subnet), Event node, edge to host-beta |
| Anchor 4 | STIG V-220812 flagged on host-alpha, CAT I, CM, CCI-000366 | STIG-Reference node + Finding node + edges |
| Anchor 5 | Escalating to P1 incident, possible lateral movement | Incident node + edges to both hosts |
| Close Act 1 | "That's six minutes of conversation. The brain has it all — typed, linked, queryable." | Open one entity (e.g., V-220812.md) — show the rich frontmatter (vuln_id, severity, severity_cat, cci, control_family — real DISA fields) |

### Act 2: Autonomous overnight worker (walkthrough, ~3 min)

| Beat | What you do | What the audience sees |
|---|---|---|
| Switch context | "Different agent. Same federal compliance shop. This one runs overnight on a cron — I'm not doing anything live." | (close Obsidian SOC vault, open `team/nlit_cora_agent/workspace/Reports/CORA-Gap-Report-{today}.md` in Obsidian or a markdown viewer) |
| Show the Schedule History card | switch to arcui → Overview tab → Schedule History card | "Last fired: today at 04:00 UTC. Status: ok. Elapsed 4s. The agent ran while I was asleep." |
| Walk the report | scroll through the report's TL;DR + section 1 | False closure on V-220812. The validator caught the date mismatch. **No human had to remember to re-check.** |
| Close | "One agent talks; one runs. Both produce institutional knowledge that didn't exist 10 minutes ago." | (graceful end) |

---

## Recovery patterns

**Agent doesn't fire write_entity:**
> "Log that — {name}, {properties}, type {Type}."

**Agent fires the wrong type:**
> "That should be a {Type}, not a {OtherType}. Try again."

**Agent over-explains or hedges:**
> Just continue with the next anchor. Don't argue with it.

**Anthropic API hiccup mid-Act-1:**
> "While we wait — the agent has already captured these on disk. Let me show you what's there." → switch to Obsidian, point at entities/

**arcui disconnects:**
> Don't react. Obsidian shows the real artifact. arcui is the sidekick view, not the deliverable.

---

## Post-demo cleanup (later, no rush)

```bash
# Stop services
pkill -f 'arc agent serve' 2>/dev/null
PID=$(lsof -iTCP:8420 -sTCP:LISTEN -t 2>/dev/null); [ -n "$PID" ] && kill $PID

# Clear the SOC entities (they're demo state, not retained knowledge)
rm -rf team/nlit_soc_agent/workspace/entities/*

# Cancel the production cron schedule (so CORA stops firing nightly until needed)
arc agent run team/nlit_cora_agent "schedule_cancel sched_80f11ca90677"

# Or — keep it running if you'll use the brain ongoing. CORA's gap reports are real institutional value.
```

---

## Known caveats (acknowledge if asked)

- Memory module's auto entity-extraction is disabled per agent config. If re-enabled, it writes flat files into `entities/` root that aren't in our typed structure. Don't enable it.
- `arc ui start` shows a `TAMPER DETECTED: hash chain broken` warning at startup if the audit chain has been edited. Investigate or rotate the chain in a federal context. For this demo it's an artifact of dev iteration; doesn't affect the demo run.
- ANTHROPIC_API_KEY is currently a shared key per build decision — accepted risk for v1. If demo traffic spikes against shared rate limits, switch to a dedicated demo key before stage.
- `[modules.memory]` writes its own context entries to `workspace/library/data/`. These are normal, not duplicates of write_entity output.

---

## Rehearsal log

Keep a running record below — what fired, what didn't, what was changed. The historical record is more valuable than any single rehearsal.

### 2026-04-28 — initial integration (this build)

- 5/5 anchors fired write_entity in the integration smoke test ($0.33, 8 turns, 5 tool calls, 8 typed entities).
- run_cora_pipeline produced the gap report with V-220812 in section 1, 6 unowned host-beta findings, 22 past-due POA&Ms.
- Schedule fired live: interval test produced 1 successful run (3.7s, "ok"). Production cron `0 4 * * *` UTC active as `sched_80f11ca90677`.

### Rehearsal 1 — _(date)_
- _Anchors fired:_ /5
- _Notes:_

### Rehearsal 2 — _(date)_
- _Anchors fired:_ /5
- _Notes:_

### Rehearsal 3 — _(date)_
- _Anchors fired:_ /5
- _Recovery line worked:_ ☐
- _Notes:_

### Rehearsal 4 — _(date)_
- _Network confirmed:_ ☐
- _Obsidian + plugin verified:_ ☐
- _arcui trace stores ≥7:_ ☐
- _Notes:_

### Rehearsal 5 — Dress _(date)_
- _Full run-through completed without restart:_ ☐
- _Anchors fired:_ /5
- _Audience-observer feedback:_
