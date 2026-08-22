# arctui

> **Build standards:** repo root [`AGENTS.md`](../../AGENTS.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Textual TUI for chatting with / monitoring a **served** agent. Viewpoint only — not the agent owner.

## Layer

**Surface / terminal UI.** Depends on `arccmd` (`arccli`), `textual`, `websockets`. Soft-registered into `arccli` as `arc tui` via `entry.py` (avoids hard circular dep: arccli soft-imports arctui; arctui hard-depends on arccmd).

## Layout

```
src/arctui/
  entry.py            # `arc tui` handler: resolve agent + gateway, open transport, run TUI
  serve.py            # Attach-or-serve resolver — ensure_gateway / Endpoint / viewer token
  gateway_client.py   # GatewayChatClient — WS ChatTransport onto /ws/chat/{agent_id}
  transport.py        # ChatTransport Protocol + TurnEvent (the render seam)
  roster.py           # resolve_agent / list_agents (arcgateway.team_roster discovery)
  trust.py            # Folder-trust — append launch dir to agent allowed_paths
  app.py              # ArcTUI (Textual App)
  transcript.py / activity.py / input_composer.py / command_completer.py
  connect.py          # Connector calls for /connect — no Textual in it
  connect_screen.py   # ConnectScreen / ConnectionsScreen modals
  prompts.py          # Approval / clarify / secret-input modals
  theme.py            # Palette tokens + TCSS
  tests/              # Colocated unit + smoke (no top-level packages/arctui/tests/)
```

## Entry points

`arc tui` via lazy registry registration in `entry.py`. Primary class: `ArcTUI`.

## Package rules

- SPEC-058: attach-or-serve gateway viewpoint — **do not construct `ArcAgent`** here (avoids a second WORM lock / dual ownership). `serve.ensure_gateway` probes `/api/health`: attach to a running gateway or spawn a detached `arc ui start --no-browser`, then attach over `/ws/chat/{agent_id}`.
- Graceful no-agent mode if the agent/gateway can't be resolved (empty/ambiguous roster, unreachable gateway).
- Talk to the gateway through the `ChatTransport` seam; render `TurnEvent`s. Don't own the agent process, and don't couple render code to the concrete client.
- D-586: `/connect` is handled in the TUI, never routed to the arccli registry —
  that handler prompts with `getpass` against the terminal Textual owns. The
  install sequence itself stays in `arcagent.modules.connectors.install`.

## Tests

`packages/arctui/src/arctui/tests/{unit,smoke}/`

## Working here

UI widgets + transport only. Register commands by importing into `COMMAND_REGISTRY`. Keep agent construction and policy in gateway/agent packages.