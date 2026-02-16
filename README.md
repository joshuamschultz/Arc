# Arc

Autonomous agent framework for building, running, and orchestrating AI agents.

## Packages

| Package | Description |
|---------|-------------|
| [arcllm](packages/arcllm/) | Unified LLM abstraction layer |
| [arcrun](packages/arcrun/) | Async execution engine |
| [arcagent](packages/arcagent/) | Autonomous agent nucleus |
| [arccli](packages/arccli/) | Unified CLI |

## Dependency Graph

```
arcllm (base)
  └── arcrun
       └── arcagent
            └── arccli
```

## Setup

```bash
uv sync
```

This installs all packages in editable mode with a single shared virtualenv.

## Individual Package Install

```bash
pip install arcllm      # just the LLM layer
pip install arcrun      # execution engine + arcllm
pip install arcagent    # full agent + arcllm + arcrun
pip install arccli      # CLI + everything
```
