# arcgateway

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Long-running daemon that makes ArcAgents reachable from chat platforms (and web) with operator-approved pairing, TaskGroup isolation, and the ArcFlow `RunnerHost` (fleet-side workflow runner lifecycle).

## Layer

**Surface / fleet service.** Funnels messaging to `arcagent`; it does not bypass the agent to call ArcRun or ArcLLM. Imported by `arcui` (data plane), adapter plugins, and `arccli`. Core has **zero** remote-platform SDKs — platforms are entry-point plugins.

## Layout

```
src/arcgateway/
  runner.py / session.py / executor*.py / cli.py / config.py
  pairing*.py / delivery.py / connect.py / fleet.py / bootstrap.py
  fs_reader.py / fs_watcher.py / team_roster.py / agent_config.py   # SPEC-022 data plane
  policy_parser.py / file_events.py
  adapters/           # base, registry, web, in_process, install — NOT telegram/slack/mattermost
  commands/           # Slash commands
  workflow_runner_host.py   # SPEC-061 RunnerHost — agent side of fleet, not arcui
```

Console script: `arcgateway` → `arcgateway.cli:main`.

## Entry points

`GatewayRunner`, `SessionRouter`, `InboundEvent`, executors, delivery types; data-plane modules (`fs_reader`, `team_roster`, …); `start_runner_host` / `RunnerHost`.

## Package rules

- **No pairing → no agent response.** User IDs hashed in allowlists.
- Use `import arcagent` and its public facade; do not couple gateway code to ArcAgent's internal layout.
- Only built-in remote-ish adapter is `web`. Telegram/Slack/Mattermost = separate packages registering on `arcgateway.adapters`.
- Federal: block unofficial plugins / missing credentials as configured.
- SPEC-022: sole read API for `team/<agent>/…` — **arcui must not touch `team/` directly**; go through `fs_reader`.
- Publish workflow runner to the agent tool surface (`set_runner`) so gateway hosting ≠ silent “no runner” on tools. Publish-before-configure ordering must not drop the runner.

## Tests

`packages/arcgateway/tests/{unit,integration,architecture,telemetry}/`

## Working here

New chat platform → **new package** + `AdapterPlugin` + `BasePlatformAdapter`, not code in this core. Pairing CLI: `arc gateway pair …`. Keep RunnerHost on the agent/fleet side so workflows run without the dashboard.
