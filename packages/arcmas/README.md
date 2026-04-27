# arcmas

Install the full Arc autonomous agent framework with a single command.

```bash
pip install arcmas
```

Arc is a security-first autonomous agent framework built for environments where audit trails, cryptographic identity, and data sovereignty are non-negotiable. Every LLM call is attributable. Every tool invocation is authorized. Every action emits a tamper-evident audit event.

## Four Pillars (ADR-019)

Every Arc agent operates under four universal guarantees at all tiers (Personal, Enterprise, Federal):

1. **Identity** — each agent carries a unique Ed25519 DID (`did:arc:{org}:{type}/{hash}`)
2. **Sign** — every pairing and delegation is cryptographically signed
3. **Authorize** — all tool calls pass through a deny-by-default policy pipeline
4. **Audit** — every operation emits a structured, hash-chained audit event

## Package map

`pip install arcmas` installs the full stack:

| Package | Purpose |
|---------|---------|
| [arctrust](../arctrust/) | Leaf library — DID identity, Ed25519 keypairs, audit emission, policy pipeline |
| [arcllm](../arcllm/) | Provider-agnostic LLM calls — 14 providers, direct HTTP, no SDKs |
| [arcrun](../arcrun/) | Async execution engine — ReAct loop, tool sandbox, streaming, spawn_many |
| [arcagent](../arcagent/) | Agent nucleus — DID-required construction, skills, extensions, session persistence |
| [arcgateway](../arcgateway/) | Multi-agent gateway — platform adapters, session routing, pairing controls |
| [arcskill](../arcskill/) | Skill management — Sigstore-verified install, scan, lock, CRL lifecycle |
| [arcteam](../arcteam/) | Team messaging — entity registry, channels, DMs, HMAC audit trail |
| [arcui](../arcui/) | Multi-agent dashboard — live WebSocket telemetry, `arc ui start/tail` |
| [arccli](../arccli/) | Unified CLI — all `arc <command>` subcommands, argparse-based |

## Quick install

```bash
# Full stack
pip install arcmas
# or with uv
uv pip install arcmas

# Individual layers
pip install arctrust   # identity + audit + policy only
pip install arcllm     # LLM abstraction only
pip install arcrun     # execution engine + arcllm + arctrust
pip install arc-agent  # full agent + arcllm + arcrun + arctrust
```

## First run

```bash
# Interactive setup wizard (tier selection, provider config)
arc init

# Create a new agent
arc agent create myagent --model anthropic/claude-sonnet-4-5-20250929

# Validate the setup
arc agent build myagent --check

# Run a one-shot task
arc agent run myagent "Summarize the CSV files in workspace/data/"

# Start chatting interactively
arc agent chat myagent
```

## Key features

- Air-gap ready: Ollama, vLLM, and HuggingFace TGI work with no internet access and no API keys
- Vault-backed secrets: API keys never touch the filesystem; TTL-cached credential resolution
- Deny-by-default tool sandbox: tools must be explicitly allowlisted; parameter-level validation on every call
- Bidirectional PII redaction: sensitive data is redacted before leaving your environment
- Progressive context management: observation masking at 70%, emergency truncation at 95%
- Multi-agent UI: `arc ui start` launches a real-time dashboard; `arc ui tail` streams events to terminal
- Federal-ready: FedRAMP, NIST 800-53, CMMC compliance mapping built in

## CLI reference

See [docs/cli.md](../../docs/cli.md) for the complete `arc <command>` reference.

## Architecture decisions

- [ADR-018](../../docs/architecture/decisions/ADR-018-no-mcp-no-migration-no-acp.md) — No MCP, No Migration, No ACP
- [ADR-019](../../docs/architecture/decisions/ADR-019-four-pillars-universal.md) — Four Pillars Universal

## License

Apache-2.0. Copyright (c) 2025-2026 BlackArc Systems.
