# arcgateway

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Long-running daemon that makes ArcAgents reachable from chat platforms (and web) with operator-approved pairing, TaskGroup isolation, and the ArcFlow `RunnerHost` (fleet-side workflow runner lifecycle).

## Layer

**Surface / fleet service.** Funnels messaging to `arcagent`; it does not bypass the agent to call ArcRun or ArcLLM. Imported by `arcui` (data plane) and `arccli`. Platform SDKs are **optional extras** (`arcgateway[telegram]`), imported lazily inside each adapter's `connect()` — never at module import — so a gateway without them still starts.

## Layout

```
src/arcgateway/
  runner.py / session.py / executor*.py / cli.py / config.py
  parts.py            # SPEC-065 COMP-001 — TextPart / MediaPart / the envelope's vocabulary
  media_store.py      # SPEC-065 COMP-002 — where an artefact lands, and its audit
  media_custody.py    # SPEC-065 COMP-002 — fetch/ceiling/refusal, and what the sender is told
  pairing*.py / delivery.py / connect.py / fleet.py / bootstrap.py
  fs_reader.py / fs_watcher.py / team_roster.py / agent_config.py   # SPEC-022 data plane
  policy_parser.py / file_events.py
  adapters/
    base.py           # the whole adapter contract + InboundDraft / PendingMedia
    _media.py         # shared media plumbing: kind_for / describe / bounded read
    registry.py       # the directory scan; _text.py — the one message splitter
    telegram/ slack/ mattermost/     # in-tree platforms, each with a PLATFORM descriptor
    web.py / in_process.py / install.py
  commands/           # Slash commands
  workflow_runner_host.py   # SPEC-061 RunnerHost — agent side of fleet, not arcui
```

Console script: `arcgateway` → `arcgateway.cli:main`.

## Entry points

`GatewayRunner`, `SessionRouter`, `InboundEvent`, executors, delivery types; data-plane modules (`fs_reader`, `team_roster`, …); `start_runner_host` / `RunnerHost`.

## Package rules

- **No pairing → no agent response.** User IDs hashed in allowlists.
- Use `import arcagent` and its public facade; do not couple gateway code to ArcAgent's internal layout.
- **A platform is a folder.** `adapters/<name>/` exporting `PLATFORM = AdapterSpec(...)` is discovered by scan; deleting the folder deletes the platform, and `registry.py` never learns either name.
- **An adapter does three things and no more** (REQ-310): lifecycle, `to_parts(payload)`, `send(target, parts)`. Download, naming, size ceilings, audit, session identity, pairing and splitting belong to the gateway — one implementation each, so a fourth platform cannot get them wrong. `tests/adapters/test_adapter_contract_surface.py` reads adapter sources for those responsibilities and fails on a second implementation.
- **Media travels as a reference, never bytes.** An adapter hands up `PendingMedia` (what it is + how to fetch it); `MediaCustodian` fetches within the ceiling, `MediaStore` writes and audits, and the agent gets a `MediaPart` pointing into its workspace.
- Federal: block unofficial platforms / missing credentials as configured.
- SPEC-022: sole read API for `team/<agent>/…` — **arcui must not touch `team/` directly**; go through `fs_reader`.
- Publish workflow runner to the agent tool surface (`set_runner`) so gateway hosting ≠ silent “no runner” on tools. Publish-before-configure ordering must not drop the runner.

## Tests

`packages/arcgateway/tests/{unit,integration,architecture,telemetry}/`, plus:

- `tests/adapters/` — the contract every discovered platform must pass, and the end-to-end wiring proof (a real payload → real handler → real router → real store → assertions on the file on disk).
- `tests/platform/{telegram,slack,mattermost}/` — per-platform suites.

## Working here

New chat platform → **new folder** under `adapters/`, exporting `PLATFORM`. Add its client to `[project.optional-dependencies]` and import it lazily inside `connect()`. Do not edit `registry.py`, and do not re-implement a gateway responsibility — the contract suite will fail you, and it is right to.

Adding an inbound kind is `to_parts`, never a handler registration: `MessageHandler(filters.TEXT, …)` is the one-line defect SPEC-065 exists to fix, and every per-layer unit test passed while photos silently did nothing.

Pairing CLI: `arc gateway pair …`. Keep RunnerHost on the agent/fleet side so workflows run without the dashboard.
