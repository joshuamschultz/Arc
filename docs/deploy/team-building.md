# Building a Team of Agents

This is the validated flow for standing up a multi-agent fleet on top of a
single-node deployment ([single-node.md](./single-node.md)) — one `arc ui
start` process serving a roster of agents, organized into a team with
channels. Validated with a live four-agent fleet (executive assistant,
coder, marketer, trader) on the same box that also routes Telegram DMs.

## 1. Create each agent

```bash
cd ~/arc
export PATH="$HOME/.local/bin:$PATH"
set -a; source ~/.arc/arc.env; set +a

.venv/bin/arc agent create josh_agent     --dir team --model anthropic/claude-sonnet-5
.venv/bin/arc agent create coder_agent    --dir team --model anthropic/claude-sonnet-5
.venv/bin/arc agent create marketer_agent --dir team --model anthropic/claude-sonnet-5
.venv/bin/arc agent create trader_agent   --dir team --model anthropic/claude-sonnet-5
```

Each mints its own DID and auto-registers with arcteam if the NATS broker
is reachable (it will be once `arc ui start` has run once, or if you start
`nats-server -js` by hand first).

## 2. Persona

Persona lives in `team/<agent>/workspace/identity.md`'s "About Me" section
— `arcagent.toml`'s `[agent]` table has no description/persona field, only
`name`/`org`/`type`/`workspace`. Keep it to a name + one-liner role
description; the agent reads this as part of its system context on every
turn, so verbosity costs tokens for no benefit:

```markdown
## About Me

**My Name:** Coder Agent

**My Role:** Software engineer agent. Writes, reviews, and refactors code;
runs tests; follows TDD and clean-code discipline. Works from specs and
reports diffs + test evidence.
```

## 3. Per-agent config deltas

Same schema as the user-wide config (see [single-node.md](./single-node.md)
§Configure), applied per-agent so the improver and policy eval run on the
right model even if the user-wide merge is ever bypassed:

```bash
.venv/bin/python scripts/deploy_node_overlays.py agent-config \
  team/coder_agent/arcagent.toml --provider anthropic --model claude-sonnet-5
```

Then validate:

```bash
.venv/bin/arc agent build team/coder_agent --check
```

Repeat for every agent. (`scripts/deploy-node.sh <agent1> <agent2> ...`
does steps 1 and 3 for a whole list of agent names in one call — it does
NOT handle persona or team/channel setup, which are one-time roster
decisions rather than repeatable bootstrap actions.)

## 4. Team + roles

`arc team create` needs an existing entity for every member ref — agents
auto-register on `arc agent create` if NATS is reachable; if not, register
manually first (`arc team register <name> --name <name> --type agent
--roles executor --workspace team/<name>/workspace`).

```bash
.venv/bin/arc team --root ~/arc/team create josh-team \
  --name "Josh's Team" \
  --channel work \
  --members agent://josh_agent,agent://coder_agent,agent://marketer_agent,agent://trader_agent
```

Member-ref format: `agent://<agent_name>` — the same string shown in the
`ID` column of `arc team entities`. Comma-separated, no spaces. This
creates the team and its first channel (`work`) in one call.

**Roles gap**: `arc agent create`'s auto-registration always sets
`roles=["executor"]` with no way to set a richer role at creation time, and
`arc team register` is not idempotent — it errors `Entity already
registered` on a duplicate DID rather than upserting. There IS an
`EntityRegistry.update()` method (`arcteam/registry.py`) with no CLI
exposed for it yet. Until a CLI lands, set proper display names/roles with
a one-off script:

```python
import asyncio
from pathlib import Path
from arccli.commands.team import _build_service, _shutdown

ROLE_UPDATES = {
    "did:arc:local:executor/<josh_did>":     ("Josh Executive Assistant", ["executive-assistant", "executor"]),
    "did:arc:local:executor/<coder_did>":    ("Coder Agent",              ["coder", "executor"]),
    "did:arc:local:executor/<marketer_did>": ("Marketer Agent",           ["marketing", "executor"]),
    "did:arc:local:executor/<trader_did>":   ("Trader Agent",             ["trader", "executor"]),
}

async def main() -> None:
    svc, registry, audit, backend = await _build_service(Path.home() / "arc" / "team")
    try:
        for did, (name, roles) in ROLE_UPDATES.items():
            entity = await registry.get(did)
            entity.name, entity.roles = name, roles
            await registry.update(entity)
    finally:
        await _shutdown(backend)

asyncio.run(main())
```

## 5. Channels

`arc team create --channel work` gives you exactly one channel. There is
**no `arc team channel create` CLI yet** — the target UX, once it lands,
should mirror `arc team create`'s shape:

```bash
# not yet implemented — this is the intended interface
arc team --root ~/arc/team channel create personal --members agent://josh_agent,agent://coder_agent,...
```

Until then, create additional channels via the same service the CLI itself
uses (`MessagingService.create_channel`), reusing `_build_service`/
`_shutdown` rather than reimplementing the NATS backend wiring:

```python
import asyncio
from pathlib import Path
from arccli.commands.team import _build_service, _shutdown
from arcteam.types import Channel

ALL_DIDS = [
    "did:arc:local:executor/<josh_did>",
    "did:arc:local:executor/<coder_did>",
    "did:arc:local:executor/<marketer_did>",
    "did:arc:local:executor/<trader_did>",
]

async def main() -> None:
    svc, registry, audit, backend = await _build_service(Path.home() / "arc" / "team")
    try:
        for name in ("personal", "brand"):
            await svc.create_channel(Channel(name=name, members=ALL_DIDS))
    finally:
        await _shutdown(backend)

asyncio.run(main())
```

## 6. Serve

One process, same as single-node deployment — no separate "start the
team" step. `arc ui start --team-root team --gateway-config
~/.arc/gateway.toml` (or the systemd unit) loads every agent under
`team/` on demand and serves the whole roster.

Remote-platform DMs (Telegram, Slack) route to exactly **one** agent —
whichever DID is in `gateway.toml`'s `[gateway].agent_did`. A multi-agent
fleet with Telegram enabled still only has one agent answering Telegram
messages; the rest are reachable via the web dashboard / API only, unless
you stand up a second gateway instance on a different bot token per
[multi-instance.md](../arcgateway/multi-instance.md).

## 7. Verify

```bash
.venv/bin/arc team --root ~/arc/team status      # Entities: N, Channels: M, Teams: 1
.venv/bin/arc team --root ~/arc/team entities     # every agent, name, roles
.venv/bin/arc team --root ~/arc/team channels     # every channel + its members

curl -s -H "Authorization: Bearer $VIEWER_TOKEN" \
  http://127.0.0.1:8420/api/team/roster            # UI-facing roster: model, provider, online status per agent
```

## Patterns for departments

Personas as one-liners, written into each agent's `workspace/identity.md`.
Tools posture is **least-privilege by default** — an agent gets zero tools
beyond the built-in scaffold (`calculate`, file ops) until you explicitly
grant more via `team/<agent>/capabilities/`. The `trader_agent` precedent:
its persona explicitly states "no trades, no financial actions, without an
explicitly granted tool... currently analysis only" — the capability
directory exists and is ready, but nothing is wired in until the operator
adds it. Model every regulated or consequential-action agent on this
pattern, not just trading.

### Procurement / inventory

| Agent | Role | Channels | Tools posture |
|---|---|---|---|
| `intake_agent` | Intake agent. Parses incoming purchase requests, validates against budget/policy, routes to the right approver. | work, procurement | Read-only on request queue; no PO creation authority. |
| `vendor_comms_agent` | Vendor-comms agent. Drafts and tracks vendor correspondence — RFQs, order confirmations, delivery follow-ups. | work, procurement | Email/messaging draft-only; sends require human approval until explicitly granted. |
| `inventory_analyst_agent` | Inventory analyst. Tracks stock levels, flags reorder points, forecasts demand from historical data. | work, procurement | Read-only on inventory DB; no write access to stock records. |
| `approvals_assistant_agent` | Approvals assistant. Summarizes pending approval requests for humans, tracks SLA on approval turnaround, escalates overdue items. | work, procurement | No approval authority itself — surfaces decisions to humans, never makes them. |

### Manufacturing

| Agent | Role | Channels | Tools posture |
|---|---|---|---|
| `line_monitor_agent` | Line monitor. Watches production line telemetry, flags anomalies, summarizes shift performance. | work, manufacturing | Read-only on line telemetry/MES data. |
| `maintenance_planner_agent` | Maintenance planner. Schedules preventive maintenance from equipment run-hours and failure history, drafts work orders. | work, manufacturing | Draft-only work order creation; dispatch requires human sign-off until granted. |
| `quality_auditor_agent` | Quality auditor. Reviews inspection data against spec, flags out-of-tolerance batches, drafts nonconformance reports. | work, manufacturing | Read-only on QA data; no authority to halt a line or reject a batch — flags for human decision. |
| `ops_assistant_agent` | Operations assistant. Summarizes daily production reports, coordinates cross-shift handoff notes. | work, manufacturing | No production-system write access; summarization and communication only. |

### Healthcare administration

| Agent | Role | Channels | Tools posture |
|---|---|---|---|
| `scheduling_agent` | Scheduling agent. Manages appointment slots, sends reminders, handles rescheduling requests within policy. | work, healthcare | Calendar read/write scoped to scheduling system only; no PHI access beyond appointment metadata. |
| `claims_billing_analyst_agent` | Claims/billing analyst. Reviews claims for coding accuracy, flags denials, drafts appeal documentation. | work, healthcare | Read-only on claims data; no submission authority — drafts route to a human biller. |
| `compliance_auditor_agent` | Compliance auditor. Checks documentation against regulatory requirements (HIPAA, CMS conditions), flags gaps. | work, healthcare | Read-only, audit-trail-only — never modifies clinical or billing records. |
| `staff_assistant_agent` | Staff assistant. Handles internal scheduling, onboarding checklists, policy Q&A for staff. | work, healthcare | No patient-data access at all — internal-facing only, scoped away from PHI systems entirely. |

For every roster above: create with `arc agent create <name>_agent --dir
team --model anthropic/claude-sonnet-5`, write the persona one-liner into
`identity.md`, apply the same `[eval]`/`[modules.skills]` deltas, add to a
department-specific channel alongside `work` (e.g. `procurement`,
`manufacturing`, `healthcare` — created the same way as `personal`/`brand`
in §5), and leave `team/<agent>/capabilities/` empty until a specific tool
grant is a deliberate, reviewed decision — not a default.
