# arccli

Unified CLI for the Arc agent platform. All commands use argparse plain handlers — no legacy Click.

## Layer position

arccli depends on arcagent, arcrun, arcllm, arcteam, arcui, arcskill, and arcgateway. It is a terminal layer: nothing depends on arccli. It installs the `arc` console script.

## What it provides

- `arc agent` — agent lifecycle: create, build, chat, run, serve, status, tools, skills, extensions, sessions, config, reload, strategies, events
- `arc llm` — LLM provider operations: version, config, providers, provider, models, validate
- `arc run` — arcrun loop without an agent directory: version, exec, task
- `arc skill` — skill management: list, create, validate, search
- `arc ext` — extension management: list, create, install, validate
- `arc team` — team messaging: status, config, init, register, entities, channels, memory-status
- `arc ui` — multi-agent dashboard: start, tail
- `arc init` — interactive first-time setup wizard; tier-based configuration (open, enterprise, federal)
- `arc gateway pair approve|list|revoke` — gateway pairing operator commands (gateway_only)
- `arc help`, `arc version`, `arc quit` — info and REPL utilities

All commands support `--json` output on data-returning subcommands for CI/CD integration.

See `docs/cli.md` for the complete command reference with arguments and examples.

## Quick example

```bash
# Create a new agent
arc agent create myagent --model anthropic/claude-sonnet-4-5-20250929

# Validate the setup
arc agent build myagent --check

# Chat interactively
arc agent chat myagent

# One-shot task
arc agent run myagent "Summarize all files in workspace/reports/"

# Start the multi-agent dashboard
arc ui start --port 8420

# Stream live events
arc ui tail --viewer-token <token> --layer llm
```

## Architecture references

- SPEC-017: Arc Core Hardening — all CLI commands migrated to argparse plain handlers
- ADR-019: Four Pillars Universal — `arc agent create` provisions DID at scaffold time

## Status

- Tests: 283 (run with `uv run --no-sync pytest packages/arccli/tests`)
- ruff + mypy --strict: clean
