# Arc CLI

Unified command-line interface for the Arc stack: [ArcLLM](https://github.com/joshuamschultz/arcllm) (provider-agnostic LLM calls), [ArcRun](https://github.com/joshuamschultz/arcrun) (agentic runtime loop), and ArcAgent (agent orchestration).

## Installation

```bash
pip install arccli
```

Development install (install dependencies first):

```bash
pip install -e /path/to/arcllm
pip install -e /path/to/arcrun
pip install -e /path/to/arccli
```

## Quick Start

```bash
# --- LLM ---
arc llm providers                         # list providers
arc llm call anthropic "Hello"            # make an LLM call

# --- Agent ---
arc agent create my-agent                 # scaffold agent directory
arc agent build my-agent                  # interactive onboarding wizard
arc agent chat my-agent                   # interactive REPL
arc agent chat my-agent --task "2+2?"     # one-shot task

# --- Run (no agent directory needed) ---
arc run task "What is 2+2?" --with-calc   # one-shot with tools
arc run exec "print(2 + 2)"              # sandboxed Python execution
arc run version                           # show arcrun info
```

## Command Groups

| Group | Purpose |
|-------|---------|
| `arc llm` | LLM provider management, model discovery, direct calls |
| `arc agent` | Agent lifecycle — create, configure, run, inspect |
| `arc run` | Direct arcrun execution without an agent directory |

## `arc llm`

| Command | Description |
|---------|-------------|
| `arc llm version` | Show version info |
| `arc llm config` | Show global ArcLLM configuration |
| `arc llm providers` | List all available providers |
| `arc llm provider NAME` | Show provider details and models |
| `arc llm models` | List all models across providers |
| `arc llm call PROVIDER PROMPT` | Make an LLM call |
| `arc llm validate` | Validate configs and API keys |

## `arc agent`

| Command | Description |
|---------|-------------|
| `arc agent create NAME` | Scaffold a new agent directory |
| `arc agent build [PATH]` | Interactive onboarding wizard (or `--check` to validate) |
| `arc agent chat [PATH]` | Interactive REPL or one-shot (`--task`) |
| `arc agent tools [PATH]` | List all tools available to an agent |
| `arc agent config [PATH]` | Show agent configuration |
| `arc agent strategies` | List available execution strategies |
| `arc agent events` | List all event types emitted by arcrun |

## `arc run`

| Command | Description |
|---------|-------------|
| `arc run task PROMPT` | Run a one-shot task with arcrun directly |
| `arc run exec CODE` | Execute Python code in a sandboxed subprocess |
| `arc run version` | Show arcrun/arcllm versions and capabilities |

Every command supports `--json` for machine-readable output. Full reference: [docs/CLI.md](docs/CLI.md).

## Agent Directory Structure

```
my-agent/
  arcagent.toml          # Agent configuration (model, tools policy, telemetry)
  workspace/
    identity.md          # System prompt (required)
    policy.md            # Behavioral constraints (optional)
    context.md           # Additional context (optional)
  tools/
    __init__.py
    example.py           # Tool definitions (exports get_tools())
```

## Requirements

- Python >= 3.11
- [arcllm](https://github.com/joshuamschultz/arcllm)
- [arcrun](https://github.com/joshuamschultz/arcrun)

## License

MIT
