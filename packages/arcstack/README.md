# arcstack

**Install the full Arc autonomous agent framework with a single command.**

```bash
pip install arcstack
```

Arc is a security-first autonomous agent framework built for environments where audit trails, cryptographic identity, and data sovereignty are non-negotiable.

## What You Get

| Package | Purpose |
|---------|---------|
| **arcllm** | Provider-agnostic LLM calls (Anthropic, OpenAI, Google, Ollama, and more) |
| **arcrun** | Async execution engine with tool sandboxing |
| **arcagent** | Agent nucleus — tools, memory, policy, identity |
| **arccli** | The `arc` CLI command |
| **arcteam** | Multi-agent team coordination |

## Quick Start

```bash
# Install
pip install arcstack

# Initialize
arc init

# Create your first agent
arc agent create myagent

# Start chatting
arc agent chat myagent
```

## Individual Packages

You can also install components separately:

```bash
pip install arcllm     # Just the LLM layer
pip install arcrun     # LLM + execution engine
pip install arcagent   # Full agent (includes arcllm + arcrun)
pip install arccli     # CLI + everything
```

## Links

- [GitHub](https://github.com/joshuamschultz/Arc)
- [Documentation](https://github.com/joshuamschultz/Arc#readme)
- [Issues](https://github.com/joshuamschultz/Arc/issues)

## License

Apache-2.0
