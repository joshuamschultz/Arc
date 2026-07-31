# PRD — Arc CLI

## Problem Statement

ArcLLM is a Python library with a rich API (config loading, provider registry, module stacking, LLM invocation). Currently, all interaction requires writing Python code. There is no way to:

- Quickly inspect configs, providers, or models from the terminal
- Make ad-hoc LLM calls without writing a script
- Validate configuration without running Python
- Explore available providers and their capabilities

A CLI eliminates this gap and provides a universal interface for developers, ops, and CI/CD pipelines.

## Goals

1. **Expose all library capabilities** via terminal commands
2. **Unified namespace** (`arc`) that works across future arc products (arcrun, arcagent)
3. **Simple and discoverable** — few commands, good `--help`, sensible defaults
4. **Machine-friendly** — `--json` flag on every command for piping/scripting
5. **Documented** — CLI README guide covering every command with examples

## Success Criteria

- [ ] `arc llm config` displays global config.toml contents
- [ ] `arc llm providers` lists all available providers in a table
- [ ] `arc llm provider <name>` shows provider settings and models
- [ ] `arc llm models` lists all models across all providers
- [ ] `arc llm call <provider> <prompt>` makes a real LLM call and prints the response
- [ ] `arc llm call` supports all module toggle flags (--retry, --telemetry, etc.)
- [ ] `arc llm validate` checks all configs and API key availability
- [ ] `arc llm version` shows version info
- [ ] `--json` works on every command
- [ ] `--help` works on every command and subcommand
- [ ] CLI README guide exists with full documentation
- [ ] All commands have tests

## Functional Requirements

| ID | Requirement | Priority | Acceptance Criteria |
|----|-------------|----------|---------------------|
| FR-01 | `arc llm config` shows global config | P0 | Displays defaults, modules, vault sections from config.toml |
| FR-02 | `arc llm config --module <name>` shows specific module config | P1 | Filters to single module section |
| FR-03 | `arc llm providers` lists providers | P0 | Table with name, api_format, default_model for each TOML in providers/ |
| FR-04 | `arc llm provider <name>` shows detail | P0 | Shows provider settings + all models with metadata |
| FR-05 | `arc llm models` lists all models | P1 | Flat table: provider, model, context_window, supports_tools, cost |
| FR-06 | `arc llm models --provider <name>` filters | P1 | Only models from specified provider |
| FR-07 | `arc llm models --tools/--vision` capability filter | P2 | Filter models by capability flags |
| FR-08 | `arc llm call <provider> <prompt>` makes LLM call | P0 | Loads model, invokes, prints response content |
| FR-09 | `arc llm call` module flags | P0 | --retry/--no-retry, --telemetry/--no-telemetry, etc. for all 7 modules |
| FR-10 | `arc llm call --model <model>` | P0 | Override default model |
| FR-11 | `arc llm call --temperature/--max-tokens` | P1 | Override generation params |
| FR-12 | `arc llm call --system <text>` | P1 | Prepend system message |
| FR-13 | `arc llm call --verbose` | P1 | Show module activity (timing, cost, tokens) |
| FR-14 | `arc llm validate` validates configs | P0 | Checks all TOMLs parse, API keys are set |
| FR-15 | `arc llm validate --provider <name>` | P1 | Validate single provider |
| FR-16 | `arc llm version` shows version | P0 | Package version, Python version |
| FR-17 | `--json` on all commands | P0 | Machine-readable JSON output |
| FR-18 | `--help` on all commands | P0 | Click auto-generates from docstrings |

## Non-Functional Requirements

| ID | Requirement | Priority | Acceptance Criteria |
|----|-------------|----------|---------------------|
| NFR-01 | Import time < 200ms for non-call commands | P1 | Lazy imports for heavy deps (httpx, otel) |
| NFR-02 | No new dependencies beyond click | P0 | Only `click` added to deps |
| NFR-03 | Extensible to future arc products | P0 | `arc run`, `arc agent` can be added by registering groups |
| NFR-04 | Works with existing config structure | P0 | No changes to config.toml or provider TOML format |

## User Stories

### US-01: Developer exploring providers
> As a developer evaluating ArcLLM, I want to list all available providers and their models so I can see what's supported without reading code.
>
> `arc llm providers` → table of providers
> `arc llm provider anthropic` → Anthropic settings + model details

### US-02: Developer making a quick call
> As a developer, I want to make a quick LLM call from the terminal to test my setup without writing a Python script.
>
> `arc llm call anthropic "What is the capital of France?"`

### US-03: Ops validating deployment
> As an ops engineer, I want to validate all configs and API keys are correct after deployment.
>
> `arc llm validate` → checks all providers, reports issues

### US-04: CI/CD pipeline integration
> As a CI pipeline, I want to validate config and check model availability in JSON format.
>
> `arc llm validate --json` → JSON output for parsing

### US-05: Developer comparing model costs
> As a developer, I want to see all models across providers with their pricing to choose the most cost-effective option.
>
> `arc llm models` → flat table with costs per provider

## Out of Scope (v1)

- Interactive/REPL mode
- Streaming output
- Log/audit/telemetry history viewing (requires storage backends)
- Multi-turn conversations
- File input for prompts (stdin piping may work naturally)
- Config editing/writing from CLI
