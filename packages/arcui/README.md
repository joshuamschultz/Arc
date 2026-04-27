# arcui

Multi-agent dashboard server. Receives live telemetry from running agents via WebSocket and serves a real-time web UI for monitoring LLM calls, tool use, and agent events.

## Layer position

arcui depends on arcllm (for `JSONLTraceStore`) and optionally arctrust (via `UIBridgeSink`). arcagent and arccli depend on arcui for the `ui_reporter` module and `arc ui` commands. arcui never imports from arcagent.

## What it provides

- `create_app` — FastAPI application factory; accepts `AuthConfig`, `max_agents`, optional `JSONLTraceStore`
- `serve` — convenience wrapper: creates app and starts uvicorn
- `attach_llm` — attach an arcllm model to the app so its traces stream to the dashboard
- `UIBridgeSink` (via `arcagent.modules.ui_reporter`) — connects arctrust audit events from a running agent to the live dashboard; enables the `arc agent serve --ui` flow

Authentication: three-token model (viewer, operator, agent); tokens are auto-generated at startup if not supplied; agent token is persisted to `~/.arcagent/ui-token` for auto-discovery.

## Quick example

```python
from arcui import create_app
import uvicorn

app = create_app()
# Tokens are printed to stdout on first run
uvicorn.run(app, host="127.0.0.1", port=8420)
```

Or from the CLI:

```bash
arc ui start --port 8420 --show-tokens
arc ui tail --viewer-token <token> --layer agent
```

## Architecture references

- SPEC-016: Multi-Agent UI — UIBridgeSink design and dashboard event schema
- ADR-019: Four Pillars Universal — every agent event observable via dashboard; audit connects to UI

## Status

- Tests: 300 (run with `uv run --no-sync pytest packages/arcui/tests`)
- ruff + mypy --strict: clean
