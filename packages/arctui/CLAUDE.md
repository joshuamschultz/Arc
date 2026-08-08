# arctui

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Textual TUI for chatting with / monitoring a **served** agent. Viewpoint only — not the agent owner.

## Layer

**Surface / terminal UI.** Depends on `arccmd` (`arccli`), `textual`, `websockets`. Soft-registered into `arccli` as `arc tui` via `entry.py` (avoids hard circular dep: arccli soft-imports arctui; arctui hard-depends on arccmd).

## Layout

```
src/arctui/
  app.py              # ArcTUI
  transcript.py / activity.py / input_composer.py
  entry.py            # Registers arc tui into COMMAND_REGISTRY
  serve.py / gateway_client.py / transport.py
  connect.py          # Connector calls for /connect — no Textual in it
  connect_screen.py   # ConnectScreen / ConnectionsScreen modals
  roster.py / trust.py / theme.py / command_completer.py
  tests/              # Colocated unit + smoke (no top-level packages/arctui/tests/)
```

## Entry points

`arc tui` via lazy registry registration in `entry.py`. Primary class: `ArcTUI`.

## Package rules

- SPEC-058: attach/spawn gateway viewpoint — **do not construct `ArcAgent`** here (avoids second WORM lock / dual ownership).
- Graceful no-agent mode if the target agent is missing.
- Talk to gateway/chat transport; don't own the agent process.
- D-586: `/connect` is handled in the TUI, never routed to the arccli registry —
  that handler prompts with `getpass` against the terminal Textual owns. The
  install sequence itself stays in `arcagent.modules.connectors.install`.

## Tests

`packages/arctui/src/arctui/tests/{unit,smoke}/`

## Working here

UI widgets + transport only. Register commands by importing into `COMMAND_REGISTRY`. Keep agent construction and policy in gateway/agent packages.
